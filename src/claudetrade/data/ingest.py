"""Ingestion: provider output into the database.

Responsibilities:

* Fetch through the configured providers, tolerating partial failure -- one
  dead provider degrades the run, it does not abort it.
* Upsert into normalised tables without ever *rewriting* history silently: when
  a provider restates a historical bar, the change is recorded as a
  data-quality event so a revision is visible rather than invisible.
* Run the quality checks and persist their findings.
* Resolve ticker mentions and classify sentiment for newly-ingested posts.

Ingestion never decides anything about trading. It only makes data available,
with its defects labelled.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from sqlalchemy import select

from claudetrade.config import AppConfig
from claudetrade.data.quality import DataQualityChecker, QualityReport
from claudetrade.db.models import (
    CorporateActionRow,
    EarningsEventRow,
    PriceBar,
    Security,
    SocialPostRow,
    SymbolAlias,
    TickerMentionRow,
)
from claudetrade.db.session import Database
from claudetrade.domain import (
    Bar,
    DataQualitySeverity,
    EarningsEvent,
    SecurityInfo,
    SocialPost,
)
from claudetrade.logging_setup import get_logger
from claudetrade.providers.base import ProviderError
from claudetrade.sentiment.entity_resolution import TickerResolver
from claudetrade.utils.timeutils import ensure_utc, utc_now

log = get_logger(__name__)

#: ``(phase, symbols_done, symbols_total) -> None`` -- see
#: ``DataIngestor.progress_callback``. Never required to succeed: a raising
#: callback is caught and logged, never allowed to break ingestion itself.
ProgressCallback = Callable[[str, int, int], None]


@dataclass(slots=True)
class IngestReport:
    """What one ingestion run achieved, and what it could not."""

    started_at: dt.datetime = field(default_factory=utc_now)
    finished_at: dt.datetime | None = None
    securities_upserted: int = 0
    bars_inserted: int = 0
    bars_revised: int = 0
    corporate_actions: int = 0
    earnings_upserted: int = 0
    posts_inserted: int = 0
    mentions_inserted: int = 0
    provider_failures: dict[str, str] = field(default_factory=dict)
    quality: QualityReport = field(default_factory=QualityReport)
    #: Posts fetched during this run, retained so the caller can build sentiment
    #: without re-querying the providers.
    posts: list[SocialPost] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """Whether any source failed. Surfaced on the dashboard."""
        return bool(self.provider_failures)

    def summary(self) -> dict[str, object]:
        return {
            "securities": self.securities_upserted,
            "bars_inserted": self.bars_inserted,
            "bars_revised": self.bars_revised,
            "earnings": self.earnings_upserted,
            "posts": self.posts_inserted,
            "mentions": self.mentions_inserted,
            "quality_errors": len(self.quality.errors),
            "quality_warnings": len(self.quality.warnings),
            "provider_failures": self.provider_failures,
        }


@dataclass(slots=True)
class _SocialFetchOutcome:
    """Result of the network-only social fetch.

    Produced entirely on the background fetch thread (see
    ``DataIngestor._start_social_fetch``) and handed to the main thread only
    after ``Thread.join()`` returns -- a single-writer-then-single-reader
    handoff with no field ever touched by both threads at once. This is what
    keeps SQLite writes single-threaded even though the *fetch* now overlaps
    the market-data phases: nothing here is persisted until it is back on
    the main thread, in ``DataIngestor._join_social_fetch``.
    """

    posts: list[SocialPost] = field(default_factory=list)
    provider_failures: dict[str, str] = field(default_factory=dict)


class DataIngestor:
    """Pulls from providers and persists into the database."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        market_provider=None,
        earnings_provider=None,
        social_providers=None,
        progress_callback: ProgressCallback | None = None,
    ):
        self.config = config
        self.db = db
        self.market = market_provider
        self.earnings = earnings_provider
        self.social = list(social_providers or [])
        self.checker = DataQualityChecker(config, db)
        #: Optional ``(phase, done, total)`` progress hook -- see
        #: ``webapi.routers.system``'s background refresh endpoint, the
        #: caller that actually uses this. The CLI's ``claudetrade refresh``
        #: passes none and is unaffected.
        self.progress_callback = progress_callback

    def _report_progress(self, phase: str, done: int, total: int) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(phase, done, total)
        except Exception:
            log.debug("progress callback raised; ignored", exc_info=True)

    # --- reference data ---------------------------------------------------

    def enrich_market_caps(
        self, securities: list[SecurityInfo], report: IngestReport
    ) -> list[SecurityInfo]:
        """Establish a real market cap for each security via the market-data
        path (ADR-0008 Decision 3's durable fix -- "computed at refresh time",
        not the packaged seed CSVs' approximate size buckets).

        Tries ``get_market_caps`` (an optional ``MarketDataProvider``
        capability -- see ``providers.base.MarketDataProvider.get_market_caps``
        and ``providers.market.yahoo.YahooMarketProvider``) against every
        configured market-data source in turn -- the primary provider, then,
        if it is a cascading ``FallbackMarketProvider``-shaped wrapper, each
        of its ``.primary``/``.fallbacks`` in order -- stopping once every
        symbol has a price. The first provider to price a symbol wins,
        matching the existing get_daily_bars/get_security_info fallback
        convention. A provider that does not support this (the default: every
        adapter except yahoo) or that errors contributes nothing and is not
        treated as a failure of the run.

        A security whose cap could NOT be established by any configured
        source is deliberately **not** dropped here and its existing
        ``market_cap_usd`` (if any) is left untouched -- only ever overwritten
        with a freshly *resolved* real figure, never cleared to ``None`` for
        having failed to re-resolve on this particular run. The gap is
        recorded as a ``unknown_market_cap`` data-quality finding instead, so
        it is visible rather than either a silent drop (survivorship-style
        bias at the universe layer) or a silently stale value. What to *do*
        about an unresolved cap (include vs exclude from the scannable
        universe) is ``UniverseSelector.for_session``'s decision, governed by
        ``UniverseConfig.unknown_cap_policy`` -- this method only establishes
        and flags, it never filters.
        """
        symbols = [s.symbol for s in securities]
        resolved: dict[str, float] = {}
        for provider in self._market_cap_sources():
            missing = [s for s in symbols if s not in resolved]
            if not missing:
                break
            try:
                caps = provider.get_market_caps(missing)
            except ProviderError as exc:
                log.warning(
                    "market-cap lookup via %s failed: %s", getattr(provider, "name", "?"), exc
                )
                continue
            except Exception:
                log.exception(
                    "unexpected error calling get_market_caps on %s", getattr(provider, "name", "?")
                )
                continue
            for symbol, cap in caps.items():
                if symbol not in resolved and cap is not None and cap > 0:
                    resolved[symbol] = cap

        enriched: list[SecurityInfo] = []
        #: Resolved THIS run (a configured provider returned a fresh figure).
        resolved_this_run = 0
        #: Not resolved this run, but already carried a cap from a prior run
        #: or a seed source -- NOT the same thing as "unresolved", and must
        #: not be counted as such: this is exactly the accounting bug a real
        #: Windows refresh surfaced ("resolved 0/2417 symbols (1 unresolved,
        #: flagged)" -- 0 resolved but only 1 flagged silently implied the
        #: other 2,416 had been resolved *this run*, when in fact they simply
        #: already had a pre-existing/seed cap that this run did nothing to).
        already_had_cap = 0
        #: Genuinely unresolved -- no cap at all, seed or fresh -- and always
        #: flagged below regardless of these counts.
        unresolved_count = 0
        for security in securities:
            cap = resolved.get(security.symbol)
            if cap is not None:
                enriched.append(replace(security, market_cap_usd=cap))
                resolved_this_run += 1
                continue
            enriched.append(security)
            if security.market_cap_usd is not None:
                already_had_cap += 1
                continue
            unresolved_count += 1
            report.quality.add(
                DataQualitySeverity.WARNING,
                "unknown_market_cap",
                f"{security.symbol}: market cap could not be established by any "
                "configured market-data provider this refresh; "
                f"unknown_cap_policy={self.config.universe.unknown_cap_policy!r} decides "
                "whether it stays in the scannable universe -- it is not silently dropped "
                "here regardless of that policy.",
                symbol=security.symbol,
            )

        # Logged unconditionally (not just when something is unresolved) so
        # the accounting always tells the truth about all three buckets,
        # including the common "nothing new resolved, but nothing is actually
        # missing either" case that a provider outage produces.
        log.info(
            "market-cap enrichment: resolved %d/%d symbols this run "
            "(%d already had a cap, %d unresolved and flagged)",
            resolved_this_run, len(securities), already_had_cap, unresolved_count,
        )
        return enriched

    def _market_cap_sources(self) -> list[object]:
        """Ordered, de-duplicated candidate providers to try for
        ``get_market_caps``.

        Duck-typed rather than importing ``FallbackMarketProvider`` (which
        lives in ``providers.registry`` and does not itself implement
        ``get_market_caps``): a cascading wrapper's ``.primary`` and
        ``.fallbacks`` are plain public attributes, so this reaches through
        one transparently whether ``self.market`` is a raw adapter or a
        wrapped one, without depending on that wrapper's internals beyond
        those two attribute names.
        """
        if self.market is None:
            return []
        candidates: list[object] = [self.market]
        primary = getattr(self.market, "primary", None)
        if primary is not None:
            candidates.append(primary)
        candidates.extend(getattr(self.market, "fallbacks", None) or [])

        seen: set[int] = set()
        out: list[object] = []
        for candidate in candidates:
            if id(candidate) in seen:
                continue
            seen.add(id(candidate))
            if callable(getattr(candidate, "get_market_caps", None)):
                out.append(candidate)
        return out

    def ingest_securities(
        self, securities: list[SecurityInfo], report: IngestReport
    ) -> list[SecurityInfo]:
        """Upsert reference data and alias rows.

        Runs market-cap enrichment first (see ``enrich_market_caps``) so the
        stored ``market_cap_usd`` reflects the real, provider-sourced figure
        wherever the market-data path could establish one -- this is what
        ``UniverseSelector.for_session`` later enforces the ADR-0008
        Decision 3 ">= $1B" floor against.
        """
        self._report_progress("securities", 0, len(securities))
        # The cap-enrichment pass below is the ~40-minute part of a first
        # refresh, and enrich_market_caps only returns when it is entirely
        # done -- so without a per-symbol hook the progress surface (webapi
        # refresh-status, hence the UI banner) sits at 0/N the whole time
        # while the provider's own console log counts up normally. Providers
        # that expose ``on_symbol_progress`` (currently TipRanksProvider)
        # get wired to the same callback for the duration of this phase.
        hooked = [
            p for p in self._market_cap_sources() if hasattr(p, "on_symbol_progress")
        ]
        total = len(securities)

        def _symbol_progress(done: int, _provider_total: int) -> None:
            self._report_progress("securities", min(done, total), total)

        for provider in hooked:
            provider.on_symbol_progress = _symbol_progress
        try:
            securities = self.enrich_market_caps(securities, report)
        finally:
            for provider in hooked:
                provider.on_symbol_progress = None
        self._report_progress("securities", len(securities), len(securities))
        with self.db.session() as session:
            for info in securities:
                row = session.get(Security, info.symbol)
                if row is None:
                    row = Security(symbol=info.symbol)
                    session.add(row)
                row.name = info.name
                row.exchange = info.exchange
                row.sector = info.sector
                row.industry = info.industry
                row.market_cap_usd = info.market_cap_usd
                row.shares_outstanding = info.shares_outstanding
                row.is_etf = info.is_etf
                row.is_leveraged_or_inverse = info.is_leveraged_or_inverse
                row.listed_date = info.listed_date
                row.delisted_date = info.delisted_date
                row.source = getattr(self.market, "name", "unknown")
                row.updated_at = utc_now()
                report.securities_upserted += 1

                # Aliases feed entity resolution: a post naming the company, or
                # its former ticker, must still resolve to the current symbol.
                aliases = {(info.name, "name")} if info.name else set()
                aliases |= {(a, "nickname") for a in info.aliases}
                aliases |= {(s, "former_symbol") for s in info.former_symbols}
                for alias, kind in aliases:
                    if not alias:
                        continue
                    normalised = _normalise_alias(alias)
                    exists = session.execute(
                        select(SymbolAlias).where(
                            SymbolAlias.symbol == info.symbol,
                            SymbolAlias.alias == alias,
                            SymbolAlias.kind == kind,
                        )
                    ).scalar_one_or_none()
                    if exists is None:
                        session.add(
                            SymbolAlias(
                                symbol=info.symbol,
                                alias=alias,
                                alias_normalised=normalised,
                                kind=kind,
                            )
                        )

        return securities

    def symbols_passing_market_cap_floor(
        self,
        symbols: list[str],
        securities: list[SecurityInfo],
    ) -> list[str]:
        """Select symbols eligible for expensive historical-data requests.

        Market-cap enrichment deliberately happens before this method. A
        current provider value therefore overrides the packaged bootstrap
        bucket, preventing a company that has fallen below the configured
        floor from consuming a Stooq history request. Unknown values follow
        the operator's explicit policy; the benchmark is always retained
        because it is needed for regime and relative-strength calculations.
        """
        by_symbol = {security.symbol: security for security in securities}
        floor = self.config.universe.min_market_cap_usd
        include_unknown = self.config.universe.unknown_cap_policy == "include"
        benchmark = self.config.market_data.benchmark_symbol
        eligible: list[str] = []
        for symbol in symbols:
            security = by_symbol.get(symbol)
            cap = security.market_cap_usd if security else None
            passes_floor = cap is not None and cap >= floor
            if symbol == benchmark or passes_floor or (cap is None and include_unknown):
                eligible.append(symbol)
        return eligible

    # --- price data --------------------------------------------------------

    def ingest_prices(
        self,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        report: IngestReport,
        *,
        securities: dict[str, SecurityInfo] | None = None,
    ) -> dict[str, list[Bar]]:
        """Fetch and persist daily bars for a set of symbols.

        Args:
            securities: Reference data used to clip each symbol's expected date
                range to its listing window, so newly-listed and delisted names
                are not reported as having missing bars.
        """
        if self.market is None:
            report.provider_failures["market_data"] = "no market-data provider configured"
            return {}

        # The benchmark (regime classification, relative-strength features)
        # is not a universe member and is typically an ETF, so it can be
        # missing from ``symbols`` -- e.g. a caller that narrowed the
        # universe before this call, or a universe source that simply never
        # lists ETFs. A real refresh log showed exactly this: "benchmark SPY
        # unavailable; regime reported as UNKNOWN for all sessions", because
        # SPY's bars were never fetched at all. This bar-fetch loop is the
        # one place that must never be missing it, regardless of what any
        # upstream caller already did or forgot to do -- deduped so a caller
        # that DID already include it (e.g. Pipeline.refresh) costs nothing
        # extra.
        benchmark = self.config.market_data.benchmark_symbol
        symbols = list(dict.fromkeys([*symbols, benchmark]))

        batch_size = max(1, self.config.market_data.max_symbols_per_request)
        collected: dict[str, list[Bar]] = {}
        total = len(symbols)
        self._report_progress("prices", 0, total)
        for i in range(0, total, batch_size):
            chunk = symbols[i : i + batch_size]
            try:
                fetched = self.market.get_daily_bars(chunk, start, end)
            except ProviderError as exc:
                log.error("market provider failed for %d symbols: %s", len(chunk), exc)
                report.provider_failures[getattr(self.market, "name", "market")] = str(exc)
                self._report_progress("prices", min(i + batch_size, total), total)
                continue
            collected.update(fetched)
            self._report_progress("prices", min(i + batch_size, total), total)

        self.checker.check_provider_gap(
            getattr(self.market, "name", "market"),
            expected=len(symbols),
            received=sum(1 for v in collected.values() if v),
            report=report.quality,
        )

        reference = securities or {}
        for symbol, bars in collected.items():
            if not bars:
                continue
            info = reference.get(symbol)
            self.checker.check_bars(
                symbol,
                bars,
                expected_start=start,
                expected_end=end,
                listed_date=info.listed_date if info else None,
                delisted_date=info.delisted_date if info else None,
                report=report.quality,
            )
            self.checker.check_staleness(symbol, bars, end, report=report.quality)
            self._persist_bars(symbol, bars, report)

        self.checker.persist(report.quality)
        return collected

    def _persist_bars(self, symbol: str, bars: list[Bar], report: IngestReport) -> None:
        """Insert new bars; flag (but still apply) restatements of existing ones."""
        source = getattr(self.market, "name", "unknown")
        with self.db.session() as session:
            existing = {
                row.session: row
                for row in session.execute(
                    select(PriceBar).where(PriceBar.symbol == symbol, PriceBar.source == source)
                ).scalars()
            }
            for bar in bars:
                row = existing.get(bar.session)
                if row is None:
                    session.add(
                        PriceBar(
                            symbol=symbol,
                            session=bar.session,
                            open=bar.open,
                            high=bar.high,
                            low=bar.low,
                            close=bar.close,
                            adj_close=bar.adj_close,
                            volume=bar.volume,
                            source=source,
                        )
                    )
                    report.bars_inserted += 1
                    continue

                changed = (
                    abs(row.close - bar.close) > 1e-9
                    or abs(row.open - bar.open) > 1e-9
                    or abs(row.high - bar.high) > 1e-9
                    or abs(row.low - bar.low) > 1e-9
                )
                if not changed:
                    continue
                # A restated historical price invalidates any signal computed
                # from the old value, so it is recorded rather than swallowed.
                report.quality.add(
                    DataQualitySeverity.WARNING,
                    "price_restated",
                    f"{symbol} {bar.session}: provider restated the bar "
                    f"(close {row.close:.4f} -> {bar.close:.4f}); signals generated from the "
                    "previous values are no longer bit-reproducible",
                    symbol=symbol,
                    session=bar.session,
                    old_close=row.close,
                    new_close=bar.close,
                )
                row.open, row.high, row.low, row.close = bar.open, bar.high, bar.low, bar.close
                row.adj_close = bar.adj_close
                row.volume = bar.volume
                row.ingested_at = utc_now()
                report.bars_revised += 1

    def ingest_corporate_actions(
        self, symbols: list[str], start: dt.date, end: dt.date, report: IngestReport
    ) -> None:
        if self.market is None:
            return
        try:
            actions = self.market.get_corporate_actions(symbols, start, end)
        except ProviderError as exc:
            report.provider_failures["corporate_actions"] = str(exc)
            return
        with self.db.session() as session:
            for symbol, items in actions.items():
                for action in items:
                    exists = session.execute(
                        select(CorporateActionRow).where(
                            CorporateActionRow.symbol == symbol,
                            CorporateActionRow.session == action.session,
                            CorporateActionRow.kind == action.kind,
                        )
                    ).scalar_one_or_none()
                    if exists is not None:
                        continue
                    session.add(
                        CorporateActionRow(
                            symbol=symbol,
                            session=action.session,
                            kind=action.kind,
                            ratio=action.ratio,
                            amount=action.amount,
                            detail=action.detail,
                            source=getattr(self.market, "name", ""),
                        )
                    )
                    report.corporate_actions += 1

    # --- earnings -----------------------------------------------------------

    def ingest_earnings(
        self, symbols: list[str], start: dt.date, end: dt.date, report: IngestReport
    ) -> dict[str, list[EarningsEvent]]:
        if self.earnings is None:
            report.provider_failures["earnings"] = "no earnings provider configured"
            return {}
        try:
            historical = self.earnings.get_historical_earnings(symbols, start, end)
            upcoming = self.earnings.get_upcoming_earnings(symbols)
        except ProviderError as exc:
            report.provider_failures[getattr(self.earnings, "name", "earnings")] = str(exc)
            return {}

        merged: dict[str, list[EarningsEvent]] = {s: list(historical.get(s, [])) for s in symbols}
        for symbol, events in upcoming.items():
            merged.setdefault(symbol, []).extend(events)

        with self.db.session() as session:
            for symbol, events in merged.items():
                for event in events:
                    source = event.source or getattr(self.earnings, "name", "")
                    exists = session.execute(
                        select(EarningsEventRow).where(
                            EarningsEventRow.symbol == symbol,
                            EarningsEventRow.report_date == event.report_date,
                            EarningsEventRow.source == source,
                        )
                    ).scalar_one_or_none()
                    if exists is not None:
                        # A date can be revised from estimated to confirmed;
                        # that is a legitimate update and is applied.
                        exists.confirmed = event.confirmed
                        exists.eps_actual = event.eps_actual
                        exists.surprise_pct = event.surprise_pct
                        continue
                    session.add(
                        EarningsEventRow(
                            symbol=symbol,
                            report_date=event.report_date,
                            session=event.session.value,
                            confirmed=event.confirmed,
                            eps_estimate=event.eps_estimate,
                            eps_actual=event.eps_actual,
                            revenue_estimate=event.revenue_estimate,
                            revenue_actual=event.revenue_actual,
                            surprise_pct=event.surprise_pct,
                            source=source,
                            as_of=ensure_utc(event.as_of) if event.as_of else utc_now(),
                        )
                    )
                    report.earnings_upserted += 1

        for symbol, events in merged.items():
            self.checker.check_earnings(symbol, events, report=report.quality)
        self.checker.persist(report.quality)
        return merged

    # --- social ---------------------------------------------------------------

    def ingest_social(
        self,
        *,
        since: dt.datetime,
        until: dt.datetime | None = None,
        symbols: list[str] | None = None,
        report: IngestReport | None = None,
    ) -> list[SocialPost]:
        """Fetch (synchronously, on this thread) and persist posts from every
        enabled social provider.

        A provider that is not configured is skipped silently -- that is the
        documented reduced-capability path, not an error. This is the fully
        sequential path: used directly whenever
        ``config.sentiment.fetch_concurrently`` is ``False``, and still the
        method any other caller gets by calling it directly. See
        ``run_full_refresh`` for the concurrent path, which fetches (via
        ``_fetch_social_posts_only``, on a background thread) while the
        market-data phases run, and only persists here-equivalent output
        once back on the main thread.
        """
        report = report or IngestReport()
        outcome = self._fetch_social_posts_only(since=since, until=until, symbols=symbols)
        report.provider_failures.update(outcome.provider_failures)
        posts = outcome.posts
        if posts:
            self._persist_posts(posts, report)
            report.posts.extend(posts)
        return posts

    def _fetch_social_posts_only(
        self,
        *,
        since: dt.datetime,
        until: dt.datetime | None,
        symbols: list[str] | None,
    ) -> _SocialFetchOutcome:
        """Fetch from every enabled social provider -- network only, no
        database access -- isolating one provider's failure from the rest.

        Safe to call from a background thread (see ``_start_social_fetch``):
        it never touches ``self.db`` or a shared ``IngestReport``, only the
        freshly-constructed ``_SocialFetchOutcome`` it returns.
        """
        outcome = _SocialFetchOutcome()
        for provider in self.social:
            name = getattr(provider, "name", "social")
            try:
                fetched = provider.fetch_posts(since=since, until=until, symbols=symbols)
            except ProviderError as exc:
                log.warning("social provider %s unavailable: %s", name, exc)
                outcome.provider_failures[name] = str(exc)
                continue
            outcome.posts.extend(fetched)
        return outcome

    def _start_social_fetch(
        self,
        *,
        since: dt.datetime,
        until: dt.datetime | None,
        symbols: list[str] | None,
    ) -> tuple[threading.Thread, list[_SocialFetchOutcome]]:
        """Kick off the network-only social fetch on a background thread.

        Returns the thread (already started) and the single-item list it
        will append its ``_SocialFetchOutcome`` to when done -- a list
        rather than a plain attribute so the "has it finished" question is
        answerable without a lock: ``run_full_refresh`` only ever reads it
        after ``thread.join()`` returns (see ``_join_social_fetch``), by
        which point the append has already happened-before that return, per
        ``threading.Thread``'s join/append happens-before guarantee. If the
        thread is still alive at the join timeout, the box is deliberately
        left unread and its eventual contents (should the thread finish
        later) are simply never collected -- the daemon thread is allowed to
        finish and be discarded, not killed, and not raced with the main
        thread's use of ``self.db``.

        A bug that raises something other than ``ProviderError`` out of
        ``_fetch_social_posts_only`` itself (not just out of one provider,
        which is already isolated there) is still caught here and turned
        into a single degraded-source entry -- the sequential path would
        have let such a bug abort the whole refresh loudly; on a background
        thread that is not an option (an unhandled exception on a thread
        just terminates it silently), so it is logged with its traceback and
        surfaced through the normal ``provider_failures``/``degraded`` path
        instead.
        """
        box: list[_SocialFetchOutcome] = []

        def _worker() -> None:
            try:
                box.append(
                    self._fetch_social_posts_only(since=since, until=until, symbols=symbols)
                )
            except Exception:
                log.exception(
                    "unexpected error in background social fetch; degrading the social "
                    "source for this refresh rather than losing it silently"
                )
                failure = _SocialFetchOutcome()
                failure.provider_failures["social_fetch"] = (
                    "unexpected error in background social fetch -- see log for traceback"
                )
                box.append(failure)

        thread = threading.Thread(
            target=_worker, name="claudetrade-social-fetch", daemon=True
        )
        log.info("social fetch started in background (%d provider(s))", len(self.social))
        thread.start()
        return thread, box

    def _join_social_fetch(
        self,
        thread: threading.Thread,
        box: list[_SocialFetchOutcome],
        report: IngestReport,
    ) -> list[SocialPost]:
        """Wait for the background social fetch and persist what it found.

        Called once the market-data phases (securities/prices/earnings) are
        done, so persistence -- and the mention resolution that follows it
        in ``run_full_refresh`` -- happens after ``ingest_securities`` has
        committed the alias table those mentions resolve against, exactly as
        it does in the sequential path. Only the *fetch* ran earlier.
        """
        timeout = self.config.sentiment.fetch_join_timeout_s
        thread.join(timeout)
        if thread.is_alive():
            log.warning(
                "social fetch still running after %.0fs timeout; proceeding with no posts "
                "from this run's background fetch -- it is left running in the background "
                "and its results are simply picked up on the next refresh instead of "
                "hanging this one",
                timeout,
            )
            return []

        outcome = box[0] if box else _SocialFetchOutcome()
        report.provider_failures.update(outcome.provider_failures)
        posts = outcome.posts
        if posts:
            self._persist_posts(posts, report)
            report.posts.extend(posts)
        ok_providers = len(self.social) - len(outcome.provider_failures)
        log.info(
            "social fetch complete: %d post(s) from %d/%d provider(s)",
            len(posts), ok_providers, len(self.social),
        )
        return posts

    def _persist_posts(self, posts: list[SocialPost], report: IngestReport) -> None:
        with self.db.session() as session:
            for post in posts:
                exists = session.execute(
                    select(SocialPostRow).where(
                        SocialPostRow.source == post.source.value,
                        SocialPostRow.external_id == post.external_id,
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    continue
                session.add(
                    SocialPostRow(
                        source=post.source.value,
                        external_id=post.external_id,
                        created_at=ensure_utc(post.created_at),
                        fetched_at=ensure_utc(post.fetched_at) if post.fetched_at else utc_now(),
                        text=post.text,
                        text_hash=post.text_hash,
                        community=post.community,
                        score=post.score,
                        num_comments=post.num_comments,
                        num_reposts=post.num_reposts,
                        num_replies=post.num_replies,
                        author_hash=post.author_hash,
                        author_age_days=post.author_age_days,
                        author_karma=post.author_karma,
                        author_followers=post.author_followers,
                        is_comment=post.is_comment,
                        parent_id=post.parent_id,
                        is_removed=post.is_removed,
                        is_crosspost=post.is_crosspost,
                        crosspost_parent=post.crosspost_parent,
                        duplicate_group=post.duplicate_group,
                        injection_risk=post.injection_risk,
                        raw_ref=post.raw_ref,
                        flair=post.flair,
                    )
                )
                report.posts_inserted += 1

    def persist_mentions(self, mentions_by_post: dict[str, list], report: IngestReport) -> None:
        """Store resolved ticker mentions keyed by post external id."""
        with self.db.session() as session:
            id_map = {
                (row.source, row.external_id): row.id
                for row in session.execute(select(SocialPostRow)).scalars()
            }
            for external_id, mentions in mentions_by_post.items():
                post_id = next(
                    (pid for (_src, ext), pid in id_map.items() if ext == external_id), None
                )
                if post_id is None:
                    continue
                for mention in mentions:
                    exists = session.execute(
                        select(TickerMentionRow).where(
                            TickerMentionRow.post_id == post_id,
                            TickerMentionRow.symbol == mention.symbol,
                        )
                    ).scalar_one_or_none()
                    if exists is not None:
                        continue
                    session.add(
                        TickerMentionRow(
                            post_id=post_id,
                            symbol=mention.symbol,
                            confidence=mention.confidence,
                            method=mention.method,
                            matched_text=getattr(mention, "matched_text", "")[:120],
                            context=getattr(mention, "context", "")[:500],
                        )
                    )
                    report.mentions_inserted += 1

    def resolve_and_persist_mentions(
        self,
        posts: list[SocialPost],
        directory: dict[str, SecurityInfo],
        report: IngestReport,
    ) -> None:
        """Resolve ticker mentions for ``posts`` and store the confident ones.

        Without this step posts land in the database with nothing linking them
        to a symbol, so sentiment aggregation finds no rows and the whole
        social half of the signal engine silently contributes nothing. Only
        mentions at or above the configured confidence floor are stored --
        below it, a "mention" is just a word that happens to look like a
        ticker.
        """
        if not posts or not directory:
            return

        resolver = TickerResolver(directory)
        floor = self.config.sentiment.min_ticker_confidence
        mentions_by_post: dict[str, list] = {}
        for post in posts:
            mentions = resolver.resolve_filtered(post, floor)
            if mentions:
                mentions_by_post[post.external_id] = mentions

        if mentions_by_post:
            self.persist_mentions(mentions_by_post, report)

    # --- orchestration ---------------------------------------------------------

    def run_full_refresh(
        self,
        *,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        securities: list[SecurityInfo] | None = None,
        social_lookback_hours: int | None = None,
    ) -> IngestReport:
        """One complete refresh cycle across every configured source.

        Reddit/news-RSS/X/Stocktwits hit entirely different hosts than the
        market-data provider (TipRanks/Yahoo), so by default
        (``config.sentiment.fetch_concurrently``, true unless overridden) the
        social *fetch* is started on a background thread before the
        securities/prices/earnings phases run, and only joined once they are
        done -- hiding the social fetch's wall-clock cost behind the market
        pass instead of paying for both in sequence. Persistence (posts,
        mentions, daily sentiment aggregates) always happens on this thread,
        after the join: SQLite writes stay single-threaded, and mention
        resolution still runs strictly after ``ingest_securities`` has
        committed the alias table it depends on -- only the network fetch
        moved earlier. Setting the config flag to ``False`` restores the
        original strictly sequential order.
        """
        report = IngestReport()
        reference = {s.symbol: s for s in (securities or [])}

        concurrent_social = bool(self.social) and self.config.sentiment.fetch_concurrently
        social_thread: threading.Thread | None = None
        social_box: list[_SocialFetchOutcome] = []
        if concurrent_social:
            since, until = _social_fetch_window(start, end, social_lookback_hours)
            # The market-cap-floor filter isn't known yet -- it depends on
            # ingest_securities, which this fetch is deliberately running
            # ahead of -- so the full candidate list is used here. Only
            # StocktwitsProvider actually reads `symbols` (as a fetch-priority
            # hint, internally capped); every other provider ignores it, so
            # this does not change what gets fetched for them.
            social_thread, social_box = self._start_social_fetch(
                since=since, until=until, symbols=list(symbols)
            )

        if securities:
            enriched = self.ingest_securities(securities, report)
            reference = {s.symbol: s for s in enriched}
            symbols = self.symbols_passing_market_cap_floor(symbols, enriched)
        self.ingest_prices(symbols, start, end, report, securities=reference)
        self.ingest_corporate_actions(symbols, start, end, report)
        self.ingest_earnings(symbols, start, end, report)

        if self.social:
            self._report_progress("sentiment", 0, 1)
            if concurrent_social and social_thread is not None:
                posts = self._join_social_fetch(social_thread, social_box, report)
            else:
                # Backfill social over the same window as the price history
                # when a lookback is not given explicitly, so a historical
                # refresh produces sentiment covering the same period as its
                # bars.
                since, until = _social_fetch_window(start, end, social_lookback_hours)
                posts = self.ingest_social(
                    since=since, until=until, symbols=symbols, report=report
                )
            self.resolve_and_persist_mentions(posts, reference, report)
            self._report_progress("sentiment", 1, 1)

        self.checker.persist(report.quality)
        report.finished_at = utc_now()
        log.info("ingestion complete: %s", report.summary())
        return report


def _social_fetch_window(
    start: dt.date, end: dt.date, social_lookback_hours: int | None
) -> tuple[dt.datetime, dt.datetime | None]:
    """The ``(since, until)`` window ``run_full_refresh`` fetches social over.

    A lookback given in hours backfills only recent activity (``until`` is
    open-ended, i.e. "up to now"); otherwise social is fetched over the same
    calendar window as the price history, so a historical refresh produces
    sentiment covering the same period as its bars.
    """
    if social_lookback_hours is not None:
        return utc_now() - dt.timedelta(hours=social_lookback_hours), None
    since = dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC)
    until = dt.datetime.combine(end, dt.time.max, tzinfo=dt.UTC)
    return since, until


def _normalise_alias(alias: str) -> str:
    """Normalise a company name for matching.

    Corporate suffixes carry no identifying information and their presence
    varies between sources, so they are stripped before comparison.
    """
    text = alias.lower().strip()
    for suffix in (
        " incorporated",
        " corporation",
        " company",
        " holdings",
        " group",
        " inc.",
        " inc",
        " corp.",
        " corp",
        " co.",
        " ltd.",
        " ltd",
        " plc",
        " llc",
        " sa",
        " nv",
        " ag",
    ):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return " ".join(text.replace(",", " ").replace(".", " ").split())
