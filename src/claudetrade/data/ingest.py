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

from sqlalchemy import func, select

from claudetrade.config import AppConfig
from claudetrade.data.quality import DataQualityChecker, QualityReport
from claudetrade.db.models import (
    CorporateActionRow,
    EarningsEventRow,
    PaperTradeRow,
    PriceBar,
    Security,
    SignalRow,
    SocialPostRow,
    SymbolAlias,
    SymbolFetchHealth,
    SymbolSentimentDaily,
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
from claudetrade.utils.timeutils import current_trading_session, ensure_utc, utc_now

log = get_logger(__name__)

#: ``(phase, symbols_done, symbols_total) -> None`` -- see
#: ``DataIngestor.progress_callback``. Never required to succeed: a raising
#: callback is caught and logged, never allowed to break ingestion itself.
ProgressCallback = Callable[[str, int, int], None]

#: Rows/items per write transaction for the bulk persistence helpers
#: (securities, earnings, posts, mentions). A single transaction across a
#: whole-universe loop held SQLite's write lock for minutes and -- because
#: WAL checkpoints only run at commit -- let the WAL balloon, degrading every
#: concurrent process's reads (QA handoff v3, F26). Chunked commits bound
#: both: the write lock is held for one chunk at a time and the WAL gets
#: checkpointed as the run progresses. All of these helpers are idempotent
#: upserts, so a run killed between chunks simply re-covers the same ground
#: on the next refresh; partial persistence was already this module's
#: documented posture (one dead provider degrades, never aborts).
PERSIST_CHUNK_ROWS = 200


def _chunks(items: list, size: int):
    """Yield ``items`` in order, ``size`` at a time."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


#: Per-symbol fetch quarantine (F23 item 4 -- the "many symbol failures"
#: retry burn): a symbol whose FULL provider chain (primary + every
#: fallback) produced zero bars this many refreshes IN A ROW stops being
#: fetched for ``_QUARANTINE_DAYS``. Deliberately conservative on both
#: sides: one refresh's outage never quarantines anything (a wholesale
#: chunk/provider failure is excluded from the per-symbol count -- see
#: ``_record_fetch_outcomes``), and expiry retries the symbol automatically,
#: with any success deleting its health row outright. Constants rather than
#: config: there is no sensible per-install tuning here, and
#: ``claudetrade db fetch-health --clear`` already covers the manual escape
#: hatch.
_QUARANTINE_AFTER_FAILURES = 3
_QUARANTINE_DAYS = 7


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
        attention_providers=None,
        progress_callback: ProgressCallback | None = None,
    ):
        self.config = config
        self.db = db
        self.market = market_provider
        self.earnings = earnings_provider
        self.social = list(social_providers or [])
        #: Aggregate mention-count sources (ApeWisdom). Kept separate from
        #: ``social`` because they return per-symbol tallies rather than
        #: posts -- see ``providers.social.apewisdom``.
        self.attention = list(attention_providers or [])
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
        # Quarantined symbols (see ``_quarantined_symbols``) are excluded
        # from the provider requests: the per-symbol dataForTicker fallback
        # inside ``get_market_caps`` is exactly the kind of doomed-retry
        # spend the quarantine exists to stop. They keep any stored cap and
        # are counted separately below -- NOT flagged ``unknown_market_cap``,
        # because "we deliberately did not ask" must not masquerade as "no
        # provider could answer"; their story is told by
        # ``claudetrade db fetch-health``.
        quarantined = self._quarantined_symbols()
        benchmark = self.config.market_data.benchmark_symbol
        skip = {s.symbol for s in securities} & quarantined - {benchmark}
        if skip:
            log.info(
                "market-cap enrichment: skipped %d quarantined symbol(s) "
                "(claudetrade db fetch-health)",
                len(skip),
            )
        symbols = [s.symbol for s in securities if s.symbol not in skip]
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
        #: Deliberately not asked about this run (fetch quarantine) -- a
        #: fourth bucket so the honest-accounting log line stays honest.
        quarantine_skipped = 0
        for security in securities:
            cap = resolved.get(security.symbol)
            if cap is not None:
                enriched.append(replace(security, market_cap_usd=cap))
                resolved_this_run += 1
                continue
            enriched.append(security)
            if security.symbol in skip:
                quarantine_skipped += 1
                continue
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
            "(%d already had a cap, %d unresolved and flagged, %d quarantined and skipped)",
            resolved_this_run, len(securities), already_had_cap, unresolved_count,
            quarantine_skipped,
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
        # One short write transaction per chunk, not one across the whole
        # ~2,400-name reference pass (see ``PERSIST_CHUNK_ROWS``). All
        # network I/O (cap enrichment above) is already done by this point;
        # nothing inside these transactions can wait on a provider.
        for chunk in _chunks(securities, PERSIST_CHUNK_ROWS):
            with self.db.session() as session:
                for info in chunk:
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
                    if info.delisted_date is not None:
                        row.delisted_date = info.delisted_date
                    # else: leave whatever is already stored untouched. Most
                    # providers' reference data never tracks a delisting date at
                    # all (info.delisted_date is simply None), and treating that
                    # absence as "confirmed still listed" would silently clear a
                    # deactivation this application made itself (see
                    # ``_deactivate_confirmed_unknown``) on the very next
                    # refresh -- undoing it before ``ingest_prices`` even runs
                    # again. Reactivation is instead the more precise trigger of
                    # "a refresh actually found real bars again" -- see
                    # ``_persist_bars``.
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

        Two F23 behaviours live here (both no-ops in their default-off
        conditions): symbols under fetch quarantine are skipped (see
        ``_quarantined_symbols``/``_record_fetch_outcomes``), and when the
        primary provider is bulk-by-date (``PolygonProvider.bulk_daily``)
        the fetch window is narrowed to the sessions the database is
        actually missing, so a normal daily refresh is one or two grouped
        calls instead of a whole re-download.

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

        # Fetch quarantine (F23 item 4): symbols whose full provider chain
        # has failed _QUARANTINE_AFTER_FAILURES refreshes running are not
        # fetched at all this run. The benchmark is exempt unconditionally
        # -- regime classification depends on it, so it always gets its
        # attempt (and its dedicated retry below) no matter what its health
        # row says. Stored history/universe membership are untouched: this
        # gates fetching only.
        quarantined = self._quarantined_symbols()
        fetch_symbols = [s for s in symbols if s == benchmark or s not in quarantined]
        skipped_count = len(symbols) - len(fetch_symbols)
        if skipped_count:
            log.info(
                "skipped %d quarantined symbol(s) with repeated full-chain fetch "
                "failures (claudetrade db fetch-health)",
                skipped_count,
            )

        # Window narrowing (F23 item 3): when the configured PRIMARY provider
        # fetches by DATE rather than by symbol (``bulk_daily = True`` --
        # currently PolygonProvider), re-requesting sessions the database
        # already stores just re-downloads the whole market for each of them.
        # Narrow to the sessions actually missing -- with one deliberate
        # deviation from "latest stored + 1": the LATEST stored session
        # itself is always re-fetched. Its stored bars may be provisional (an
        # intraday GetQuotes current-session merge, persisted by an earlier
        # run today -- see ``_merge_current_session_bars``), and skipping
        # past it would freeze that partial bar as the permanent record; the
        # provider's per-date response cache makes the repair pass free
        # whenever that date was already grouped-fetched once. Full window
        # stays when no bulk provider is primary (per-symbol providers pay
        # per symbol, not per date -- narrowing would only shrink coverage
        # repair, not save calls) or when the DB has no bars at all.
        effective_start = start
        bulk = self._bulk_primary_provider()
        if bulk is not None:
            latest = self._latest_stored_bar_session()
            # Only a "bring me current" window is narrowed (start < latest
            # <= end). A window ending BEFORE the latest stored session is an
            # explicit historical request -- the operator wants those exact
            # dates, and "the DB has newer bars" says nothing about whether
            # it has THOSE; the per-date cache keeps honouring it cheap.
            if latest is not None and start < latest <= end:
                effective_start = latest
                log.info(
                    "bulk-daily provider %s is primary: narrowed the price fetch window "
                    "%s..%s -> %s..%s (DB already has bars through %s; the latest stored "
                    "session is re-fetched so a provisional current-session bar gets "
                    "repaired)",
                    getattr(bulk, "name", "?"), start, end, effective_start, end, latest,
                )

        batch_size = max(1, self.config.market_data.max_symbols_per_request)
        collected: dict[str, list[Bar]] = {}
        #: Symbols whose whole chunk failed with a ProviderError -- an outage
        #: signal, never counted as a per-symbol failure (see
        #: ``_record_fetch_outcomes``).
        chain_failed: set[str] = set()
        attempted: list[str] = []
        total = len(fetch_symbols)
        self._report_progress("prices", 0, total)
        for i in range(0, total, batch_size):
            chunk = fetch_symbols[i : i + batch_size]
            attempted.extend(chunk)
            try:
                fetched = self.market.get_daily_bars(chunk, effective_start, end)
            except ProviderError as exc:
                log.error("market provider failed for %d symbols: %s", len(chunk), exc)
                report.provider_failures[getattr(self.market, "name", "market")] = str(exc)
                chain_failed.update(chunk)
                self._report_progress("prices", min(i + batch_size, total), total)
                continue
            collected.update(fetched)
            self._report_progress("prices", min(i + batch_size, total), total)

        # The merged-batch inclusion above is harmless when it works, but it
        # ties the benchmark's fate to whatever else lands in its batch: a
        # real refresh log showed 641/1,673 symbols (SPY included) degrade to
        # close-only fallback bars because one batch's failure cascaded the
        # whole chunk to the fallback provider. This dedicated, independent,
        # single-symbol call is the actual guarantee. It uses the same
        # effective (possibly narrowed) window as the batches: with a bulk
        # primary, a full-window benchmark fetch would pay one grouped call
        # per already-stored date for nothing.
        self._ensure_benchmark_bars(benchmark, effective_start, end, collected, report)

        self._merge_current_session_bars(fetch_symbols, collected)

        self.checker.check_provider_gap(
            getattr(self.market, "name", "market"),
            expected=len(attempted),
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
                # The narrowed window, not the requested one: sessions before
                # effective_start were deliberately not fetched this run, and
                # flagging them missing would report the narrowing itself as
                # a data defect.
                expected_start=effective_start,
                expected_end=end,
                listed_date=info.listed_date if info else None,
                delisted_date=info.delisted_date if info else None,
                report=report.quality,
            )
            self.checker.check_staleness(symbol, bars, end, report=report.quality)
            self._persist_bars(symbol, bars, report)

        self._record_fetch_outcomes(
            attempted=attempted, chain_failed=chain_failed, collected=collected, report=report
        )
        self._deactivate_confirmed_unknown(fetch_symbols, report)

        self.checker.persist(report.quality)
        return collected

    # --- fetch health / quarantine (F23 item 4) -----------------------------

    def _quarantined_symbols(self) -> set[str]:
        """Symbols currently under fetch quarantine (``quarantined_until`` in
        the future). Defensive throughout: no database, a missing table (an
        un-migrated store), or any read error just means an empty set -- the
        quarantine is an optimisation and must never be able to fail a
        refresh."""
        if self.db is None:
            return set()
        now = utc_now()
        try:
            with self.db.read_session() as session:
                rows = session.execute(
                    select(SymbolFetchHealth.symbol, SymbolFetchHealth.quarantined_until)
                ).all()
        except Exception:
            log.debug("could not read symbol_fetch_health; no quarantine applied", exc_info=True)
            return set()
        out: set[str] = set()
        for symbol, until in rows:
            if until is None:
                continue
            if until.tzinfo is None:
                # SQLite hands DateTime(timezone=True) back naive; every
                # write goes through utc_now(), so re-attaching UTC
                # reproduces the stored instant (same convention as the
                # signals ledger's read path).
                until = until.replace(tzinfo=dt.UTC)
            if until > now:
                out.add(symbol)
        return out

    def _record_fetch_outcomes(
        self,
        *,
        attempted: list[str],
        chain_failed: set[str],
        collected: dict[str, list[Bar]],
        report: IngestReport,
    ) -> None:
        """Update ``symbol_fetch_health`` from this refresh's bar outcomes.

        Success (any bars, from any provider in the chain or the GetQuotes
        current-session merge) DELETES the symbol's health row -- the table
        only ever holds currently-failing names, which keeps both this
        method's full-table read and ``claudetrade db fetch-health``'s
        listing trivially small. Failure means the symbol was attempted
        through the whole chain and yielded nothing; a wholesale chunk
        failure (``chain_failed`` -- one ProviderError covering many
        symbols) is an outage signal and deliberately counts for nobody,
        otherwise three bad refreshes would quarantine the entire universe.
        One short transaction over a handful of rows -- never held across
        any network fetch.
        """
        if self.db is None:
            return
        succeeded = {s for s, bars in collected.items() if bars}
        failed = [s for s in attempted if s not in succeeded and s not in chain_failed]
        if not succeeded and not failed:
            return
        now = utc_now()
        newly_quarantined: list[str] = []
        try:
            with self.db.session() as session:
                rows = {
                    r.symbol: r
                    for r in session.execute(select(SymbolFetchHealth)).scalars()
                }
                for symbol in succeeded:
                    row = rows.get(symbol)
                    if row is not None:
                        session.delete(row)
                for symbol in failed:
                    row = rows.get(symbol)
                    if row is None:
                        row = SymbolFetchHealth(symbol=symbol, consecutive_failures=0)
                        session.add(row)
                        rows[symbol] = row
                    row.consecutive_failures += 1
                    row.last_failure_at = now
                    row.last_error = (
                        "no bars from any configured market-data provider this refresh"
                    )
                    row.updated_at = now
                    if row.consecutive_failures >= _QUARANTINE_AFTER_FAILURES:
                        previously = row.quarantined_until
                        if previously is not None and previously.tzinfo is None:
                            previously = previously.replace(tzinfo=dt.UTC)
                        row.quarantined_until = now + dt.timedelta(days=_QUARANTINE_DAYS)
                        if previously is None or previously <= now:
                            newly_quarantined.append(symbol)
        except Exception:
            log.debug("could not update symbol_fetch_health; skipping", exc_info=True)
            return

        if failed:
            log.info(
                "fetch health: %d symbol(s) failed the full provider chain this refresh, "
                "%d newly quarantined for %d days (claudetrade db fetch-health)",
                len(failed), len(newly_quarantined), _QUARANTINE_DAYS,
            )
        if newly_quarantined:
            shown = ", ".join(sorted(newly_quarantined)[:20])
            more = len(newly_quarantined) - min(len(newly_quarantined), 20)
            report.quality.add(
                DataQualitySeverity.WARNING,
                "symbol_quarantined",
                f"{len(newly_quarantined)} symbol(s) quarantined for {_QUARANTINE_DAYS} days "
                f"after {_QUARANTINE_AFTER_FAILURES} consecutive refreshes with no bars from "
                f"any configured provider: {shown}"
                + (f" (+{more} more)" if more else "")
                + ". Inspect or clear with 'claudetrade db fetch-health'.",
            )

    def _bulk_primary_provider(self) -> object | None:
        """The configured PRIMARY provider iff it declares bulk-by-date
        semantics (``bulk_daily = True``) and is actually configured.

        Reaches through a ``FallbackMarketProvider``-shaped wrapper's
        ``.primary`` the same duck-typed way ``_market_cap_sources`` does.
        Fallback positions deliberately do not count: narrowing only pays
        off when the provider doing the heavy lifting is per-date, and an
        unconfigured bulk primary means the per-symbol fallbacks are doing
        the work -- narrowing their window would shrink coverage repair
        without saving a single call.
        """
        if self.market is None:
            return None
        primary = getattr(self.market, "primary", None) or self.market
        if not getattr(primary, "bulk_daily", False):
            return None
        try:
            status = primary.status()
        except Exception:
            log.debug("bulk provider status() raised; treating as unconfigured", exc_info=True)
            return None
        return primary if getattr(status, "configured", False) else None

    def _latest_stored_bar_session(self) -> dt.date | None:
        """Most recent session with ANY stored price bar, across all symbols
        and sources -- the right key for per-DATE narrowing (a grouped call
        covers every symbol at once, so per-symbol gaps older than this are
        ``claudetrade db backfill``'s job, not the daily refresh's)."""
        if self.db is None:
            return None
        try:
            with self.db.read_session() as session:
                return session.execute(select(func.max(PriceBar.session))).scalar()
        except Exception:
            log.debug("could not read latest stored bar session", exc_info=True)
            return None

    def _ensure_benchmark_bars(
        self,
        benchmark: str,
        start: dt.date,
        end: dt.date,
        collected: dict[str, list[Bar]],
        report: IngestReport,
    ) -> None:
        """Guarantee an independent attempt to fetch the benchmark's bars.

        Only attempted when the merged-batch pass above did not already
        yield real bars for ``benchmark`` -- the common case costs nothing
        extra. When it was missing, this issues one dedicated, single-symbol
        ``get_daily_bars`` call, entirely independent of whatever else was
        in the benchmark's batch, so an unrelated failure elsewhere in that
        batch cannot take the benchmark down with it. If bars are still
        unavailable after this dedicated attempt -- i.e. no configured
        source had anything for it -- an ERROR (not a warning) is logged,
        because ``Pipeline.classify_regimes`` will report every session's
        regime as UNKNOWN without it, and that is a materially worse outcome
        than an ordinary missing-bars warning for an arbitrary symbol.
        """
        if collected.get(benchmark):
            return
        if self.market is None:
            return
        try:
            fetched = self.market.get_daily_bars([benchmark], start, end)
        except ProviderError as exc:
            log.error(
                "dedicated benchmark fetch for %s failed: %s -- regime classification will be "
                "reported as UNKNOWN for every session in this run because no bars are "
                "available for the benchmark",
                benchmark, exc,
            )
            report.provider_failures.setdefault(f"benchmark:{benchmark}", str(exc))
            return

        bars = fetched.get(benchmark) or []
        if bars:
            collected[benchmark] = bars
            log.info(
                "dedicated benchmark fetch recovered %d bar(s) for %s", len(bars), benchmark
            )
            return

        log.error(
            "benchmark %s has no bars from any configured market-data source, even after a "
            "dedicated fetch attempt -- regime classification will be reported as UNKNOWN for "
            "every session in this run",
            benchmark,
        )

    def _merge_current_session_bars(
        self, symbols: list[str], collected: dict[str, list[Bar]]
    ) -> None:
        """Fill in today's daily bar from TipRanks GetQuotes for any symbol
        whose already-collected series (Yahoo's historical chart, primarily
        -- see ``providers.market.yahoo``) has nothing for today yet.

        **Merge rule -- deliberately CONSERVATIVE (owner-directed; see this
        change's own report for why)**: this only ever APPENDS a
        GetQuotes-derived bar for a session date that is not already present
        in ``collected[symbol]``. It never replaces, overwrites, or prefers
        the GetQuotes bar over an existing one, even for today's own session
        -- e.g. it does NOT implement "prefer GetQuotes over Yahoo when the
        market is open or GetQuotes is newer" at all. Getting that richer
        rule right (avoiding look-ahead, avoiding a downstream signal being
        computed from one value and then silently recomputed from a
        different one later in the same run) was judged not worth the risk
        for this change; the safe subset -- "fill in today's bar only when
        NOTHING else has any bar for that exact session at all" -- already
        eliminates the common gap this exists for (Yahoo's chart endpoint
        typically lagging by one session until after that day's close)
        without any chance of silently overwriting a value a quality check
        or a strategy signal may already have read earlier in this same run.

        Deduped by exact session-date equality: a GetQuotes bar is appended
        for symbol X only if ``collected[X]`` has no existing bar whose
        ``.session`` matches it -- true whether that existing bar came from
        Yahoo, TipRanks' own close-only last-resort bars, or a prior call
        within this same run. A pre-filter (only symbols with no bar dated
        exactly "today" per this process's own UTC clock) limits which
        symbols GetQuotes is even queried for, purely to save calls; a
        symbol wrongly included by that heuristic (e.g. a timezone edge
        case) is still perfectly safe -- the dedupe check above is exact and
        catches it regardless.

        A no-op with zero network calls when there is no ``tipranks``
        provider in the configured chain, or when it does not expose
        ``get_current_session_bars`` (e.g. an older/foreign stand-in used in
        a test), or when ``TipRanksConfig.use_getquotes_batch`` is off (in
        which case ``get_current_session_bars`` itself already returns
        nothing without any HTTP call -- see
        ``providers.market.tipranks.TipRanksProvider.get_quotes``).
        """
        tipranks = self._market_provider_named("tipranks")
        get_current = getattr(tipranks, "get_current_session_bars", None)
        if not callable(get_current):
            return

        # The ET trading session, not the UTC date: after Friday's close the
        # UTC date is Saturday, and "which symbols lack a bar for *today*"
        # keyed on Saturday would select the entire universe for a GetQuotes
        # sweep that can only ever return Friday-stamped bars.
        today = current_trading_session()
        missing_today = [
            symbol
            for symbol in symbols
            if not any(b.session == today for b in (collected.get(symbol) or []))
        ]
        if not missing_today:
            return

        try:
            current_bars = get_current(missing_today)
        except ProviderError as exc:
            log.debug("tipranks current-session bar merge failed: %s", exc)
            return
        except Exception:
            log.debug("tipranks current-session bar merge raised unexpectedly", exc_info=True)
            return

        filled = 0
        for symbol, bar in (current_bars or {}).items():
            existing = collected.setdefault(symbol, [])
            if any(b.session == bar.session for b in existing):
                continue  # dedupe by session date -- never a duplicate/overwrite
            existing.append(bar)
            existing.sort(key=lambda b: b.session)
            filled += 1

        if filled:
            log.info(
                "current-session bars: filled %d symbol(s) from tipranks GetQuotes "
                "(conservative merge -- appended only where the daily-history source had "
                "no bar for that exact session at all)",
                filled,
            )

    def _market_provider_named(self, name: str) -> object | None:
        """Find a provider instance by ``.name`` within ``self.market``.

        Reaches through a ``FallbackMarketProvider``-shaped wrapper's
        ``.primary``/``.fallbacks`` the same way ``_market_cap_sources``
        does -- duck-typed rather than importing ``FallbackMarketProvider``
        (``providers.registry``), which does not itself carry a ``.name``
        matching any real adapter's.
        """
        if self.market is None:
            return None
        candidates: list[object] = [self.market]
        primary = getattr(self.market, "primary", None)
        if primary is not None:
            candidates.append(primary)
        candidates.extend(getattr(self.market, "fallbacks", None) or [])
        for candidate in candidates:
            if getattr(candidate, "name", None) == name:
                return candidate
        return None

    def _deactivate_confirmed_unknown(self, symbols: list[str], report: IngestReport) -> None:
        """Mark a symbol inactive when BOTH tipranks and yahoo report it
        unknown in this same refresh, and it has no recently-stored bars.

        Both adapters keep a per-refresh ``_not_found`` set of symbols they
        had no data for this run (see
        ``providers.market.tipranks.TipRanksProvider._not_found`` and
        ``providers.market.yahoo.YahooMarketProvider._not_found``) -- a real
        refresh log showed WBA/JNPR/SNV/HES/HOLX/ATA/INE hitting this on
        every single refresh (a confirmed HTTP 404 from yahoo and no
        analyst/overview coverage from tipranks), burning an API call and
        polluting a batch every time for names that are, in fact, gone.

        Deliberately conservative -- a single provider hiccup must never
        deactivate a live name: both sources must agree in the SAME refresh
        AND the symbol must have no ``price_bars`` row in the last 30 days.
        Only ``Security.delisted_date`` is touched (the same column
        ``UniverseSelector.for_session``/``SecurityInfo.is_active_on``
        already use for point-in-time universe membership) -- never a new,
        parallel flag -- and only for a security that is not already marked
        inactive. Already-stored history is untouched, so a backtest
        spanning the period the symbol WAS listed still sees it; this only
        stops it from being offered to today's *scannable* universe.

        Every symbol deactivated this way gets a WARNING data-quality
        finding, so the change is visible, never silent.

        This method does not itself stop a future refresh from attempting
        the symbol again -- ``ingest_securities`` no longer clobbers an
        existing ``delisted_date`` back to ``None`` from a source that
        simply does not track delisting (see its docstring), but nothing
        here filters the symbol out of a future ``get_daily_bars`` call
        either. If a future refresh's fetch succeeds, ``_persist_bars``
        clears ``delisted_date`` again automatically -- that is
        reactivation's entire mechanism, deliberately with no separate flag
        or scheduled re-probe to keep in sync.
        """
        tipranks = self._market_provider_named("tipranks")
        yahoo = self._market_provider_named("yahoo")
        if tipranks is None or yahoo is None:
            return
        tipranks_unknown = getattr(tipranks, "_not_found", None)
        yahoo_unknown = getattr(yahoo, "_not_found", None)
        if not tipranks_unknown or not yahoo_unknown:
            return

        both_unknown = sorted((tipranks_unknown & yahoo_unknown) & set(symbols))
        if not both_unknown:
            return

        today = utc_now().date()
        cutoff = today - dt.timedelta(days=30)
        with self.db.session() as session:
            for symbol in both_unknown:
                row = session.get(Security, symbol)
                if row is None or row.delisted_date is not None:
                    continue  # nothing known about it, or already inactive
                recent = session.execute(
                    select(PriceBar.id)
                    .where(PriceBar.symbol == symbol, PriceBar.session >= cutoff)
                    .limit(1)
                ).first()
                if recent is not None:
                    # A provider hiccup must not deactivate a name that was
                    # trading as recently as last month.
                    continue
                row.delisted_date = today
                report.quality.add(
                    DataQualitySeverity.WARNING,
                    "symbol_deactivated",
                    f"{symbol}: both tipranks and yahoo report this ticker unknown this "
                    f"refresh, and no price bars have been stored in the last 30 days; "
                    f"marked delisted_date={today} so it drops out of the point-in-time-active "
                    "universe. Reactivates automatically if a future refresh finds real bars "
                    "for it again.",
                    symbol=symbol,
                )

    def _persist_bars(self, symbol: str, bars: list[Bar], report: IngestReport) -> None:
        """Insert new bars; flag (but still apply) restatements of existing ones.

        Only ever called with a non-empty ``bars`` (see ``ingest_prices``'s
        ``if not bars: continue`` guard), so reaching this method at all
        means a provider found real data for ``symbol`` this refresh. That
        is exactly the trigger this application uses for reactivation: if
        the security was previously marked ``delisted_date`` by
        ``_deactivate_confirmed_unknown`` (a provider hiccup must not
        strand a name that comes back to life), it is cleared here so the
        symbol re-enters the point-in-time-active set
        ``UniverseSelector.for_session``/``SecurityInfo.is_active_on`` check.
        """
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

            security = session.get(Security, symbol)
            if security is not None and security.delisted_date is not None:
                previous = security.delisted_date
                security.delisted_date = None
                report.quality.add(
                    DataQualitySeverity.INFO,
                    "symbol_reactivated",
                    f"{symbol}: real bars found again this refresh (was marked "
                    f"delisted_date={previous}); delisted_date cleared and the symbol "
                    "re-enters the point-in-time-active universe.",
                    symbol=symbol,
                )

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

        # Both provider calls above are complete before any transaction
        # opens; commits land per symbol-chunk rather than once for the whole
        # universe (see ``PERSIST_CHUNK_ROWS``).
        for symbol_chunk in _chunks(list(merged.items()), PERSIST_CHUNK_ROWS):
            with self.db.session() as session:
                for symbol, events in symbol_chunk:
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

    def _social_symbol_hints(self, candidates: list[str] | None) -> list[str] | None:
        """Per-cycle symbol priority hints for the social providers (QA F22).

        Only ``StocktwitsProvider`` reads the ``symbols`` hint (its per-cycle
        cap fetches the FIRST ``max_symbols_per_cycle`` entries); every other
        social provider ignores it. With ``stocktwits.watchlist_symbols`` at
        its default ``[]`` the hint used to be whatever order the universe
        arrived in, so the capped budget was spent on an arbitrary
        alphabetical-ish slice -- or, for callers passing no hint at all, on
        nothing whatsoever. This method makes the empty-watchlist default
        useful by seeding the front of the hint list from what the operator
        demonstrably cares about, in priority order, deduped:

        1. open paper-portfolio holdings (positions being actively tracked),
        2. symbols from ledger signals over the last ~7 sessions,
        3. top stored trending symbols by 7-day post volume -- restricted by
           a ``securities`` join exactly like ``mcp_server.get_trending``,
           so junk symbols from pre-fix extraction can never steer fetching
           (and post-rebuild the stored rows are clean anyway).

        The seed is capped at the Stocktwits per-cycle budget; the caller's
        own candidates follow it so any remaining budget behaves exactly as
        before. A configured NON-empty watchlist returns ``candidates``
        untouched -- the operator's explicit list keeps the exact pre-change
        behaviour (including the provider's own watchlist fallback when no
        hint is passed). Read-only, best-effort: any query failure degrades
        to the unseeded hint rather than failing a refresh over fetch
        prioritisation.
        """
        if self.config.stocktwits.watchlist_symbols:
            return candidates
        try:
            seeded = self._seed_social_symbols()
        except Exception:
            log.warning("social symbol-hint seeding failed; using unseeded hints", exc_info=True)
            return candidates
        if not seeded:
            return candidates
        log.info(
            "stocktwits watchlist is empty; seeded %d priority symbol(s) for this "
            "cycle from open positions / recent signals / stored trending: %s",
            len(seeded),
            ", ".join(seeded),
        )
        remainder = [s for s in (candidates or []) if s not in set(seeded)]
        return seeded + remainder

    def _seed_social_symbols(self) -> list[str]:
        """The seed list itself: holdings, then recent signals, then trending.

        Deduped preserving first (highest-priority) occurrence and capped to
        ``stocktwits.max_symbols_per_cycle`` -- there is no point ranking
        more names than the one provider that consumes the hint can fetch.
        """
        cap = max(0, self.config.stocktwits.max_symbols_per_cycle)
        today = utc_now().date()
        window_start = today - dt.timedelta(days=7)
        with self.db.read_session() as session:
            holdings = list(
                session.execute(
                    select(PaperTradeRow.symbol)
                    .where(PaperTradeRow.exit_session.is_(None))
                    .order_by(PaperTradeRow.entry_session.desc(), PaperTradeRow.symbol)
                ).scalars()
            )
            # "Recent" is both senses at once: the last 7 distinct signal
            # sessions, AND within the last 14 calendar days (~7 trading
            # sessions plus weekend slack) -- a ledger idle for months must
            # not keep steering the fetch budget toward long-expired ideas.
            recent_sessions = list(
                session.execute(
                    select(SignalRow.session)
                    .where(SignalRow.session >= today - dt.timedelta(days=14))
                    .distinct()
                    .order_by(SignalRow.session.desc())
                    .limit(7)
                ).scalars()
            )
            recent_signals: list[str] = []
            if recent_sessions:
                recent_signals = list(
                    session.execute(
                        select(SignalRow.symbol)
                        .where(SignalRow.session.in_(recent_sessions))
                        .order_by(SignalRow.session.desc(), SignalRow.overall_score.desc())
                    ).scalars()
                )
            trending = list(
                session.execute(
                    select(SymbolSentimentDaily.symbol)
                    # Same guard as mcp_server.get_trending: only symbols the
                    # reference table knows may steer the fetch budget.
                    .join(Security, Security.symbol == SymbolSentimentDaily.symbol)
                    .where(
                        SymbolSentimentDaily.source == "all",
                        SymbolSentimentDaily.session >= window_start,
                    )
                    .group_by(SymbolSentimentDaily.symbol)
                    .order_by(func.sum(SymbolSentimentDaily.post_count).desc())
                    .limit(cap)
                ).scalars()
            )
        seen: set[str] = set()
        seeded: list[str] = []
        for symbol in [*holdings, *recent_signals, *trending]:
            if symbol and symbol not in seen:
                seen.add(symbol)
                seeded.append(symbol)
        return seeded[:cap]

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

        ``symbols`` is a fetch-priority hint (only Stocktwits consumes it);
        with an empty configured Stocktwits watchlist it is seeded from open
        positions / recent signals / stored trending first -- see
        ``_social_symbol_hints``.
        """
        report = report or IngestReport()
        symbols = self._social_symbol_hints(symbols)
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
        # Fetch already happened (possibly on the background thread); commits
        # land per chunk so a many-thousand-post persist never holds the
        # write lock end to end (see ``PERSIST_CHUNK_ROWS``).
        for chunk in _chunks(posts, PERSIST_CHUNK_ROWS):
            with self.db.session() as session:
                for post in chunk:
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
                            fetched_at=ensure_utc(post.fetched_at)
                            if post.fetched_at
                            else utc_now(),
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
        """Store resolved ticker mentions keyed by post external id.

        The external-id -> post-id map is read once, in its own read
        transaction, as (external_id, id) pairs only -- this used to load
        every column of every stored post and then linear-scan that map per
        mention inside one whole-batch WRITE transaction, i.e. O(posts x
        mentions) of pure Python compute performed while holding the write
        lock. First match per external id is kept, preserving the previous
        scan's first-hit-in-insertion-order behaviour for the (unindexed)
        edge case of one external id appearing under two sources.
        """
        with self.db.read_session() as session:
            id_map: dict[str, int] = {}
            for external_id, post_id in session.execute(
                select(SocialPostRow.external_id, SocialPostRow.id)
            ):
                id_map.setdefault(external_id, post_id)

        for chunk in _chunks(list(mentions_by_post.items()), PERSIST_CHUNK_ROWS):
            with self.db.session() as session:
                for external_id, mentions in chunk:
                    post_id = id_map.get(external_id)
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

    def ingest_attention(
        self, session_date: dt.date, report: IngestReport | None = None
    ) -> int:
        """Fetch aggregate mention counts and store them as attention rows.

        Written into ``symbol_sentiment_daily`` under their own ``source``
        labels (``apewisdom:all-stocks``, ``apewisdom:4chan``, ...), which is
        what keeps them out of the strategy path: ``data.context.
        _sentiment_for`` scores against the combined ``"all"`` row, and these
        deliberately never write it. Nothing here can therefore move a
        signal's score -- it adds a visible attention series (trending,
        per-source breakdowns, diagnostics) without silently rewiring how
        candidates are ranked.

        Only the attention fields are populated. ``raw_sentiment``,
        ``bull_bear_ratio`` and every other polarity column keep their
        neutral defaults because the source carries no direction at all, and
        ``unique_authors`` stays 0 because an aggregate tally genuinely does
        not know how many distinct people spoke -- a guess there would feed
        the manipulation model fiction. ``is_sufficient`` consequently reads
        ``False`` for these rows, which is correct: they are not a
        polarity sample.

        Rows are only stored for symbols present in ``securities``, the same
        guard ``mcp_server.get_trending`` applies -- an aggregator naming a
        ticker this installation does not track is not evidence about
        anything it screens.

        Best-effort throughout: a provider failure is recorded on the report
        and the refresh continues.
        """
        report = report or IngestReport()
        if not self.attention:
            return 0

        observations: list = []
        for provider in self.attention:
            name = getattr(provider, "name", "attention")
            try:
                observations.extend(provider.fetch_attention())
            except Exception as exc:
                log.warning("attention provider %s failed: %s", name, exc)
                report.provider_failures[name] = str(exc)
        if not observations:
            return 0

        with self.db.read_session() as session:
            known = {
                row[0]
                for row in session.execute(select(Security.symbol)).all()
            }

        written = 0
        for chunk in _chunks(observations, PERSIST_CHUNK_ROWS):
            with self.db.session() as session:
                for obs in chunk:
                    if obs.symbol not in known:
                        continue
                    source = f"apewisdom:{obs.community}"
                    existing = (
                        session.query(SymbolSentimentDaily)
                        .filter_by(symbol=obs.symbol, session=session_date, source=source)
                        .one_or_none()
                    )
                    row = existing or SymbolSentimentDaily(
                        symbol=obs.symbol, session=session_date, source=source
                    )
                    row.post_count = obs.mentions
                    row.total_engagement = float(obs.upvotes)
                    row.mention_acceleration = obs.mention_acceleration
                    row.labels = {
                        "attention_only": 1.0,
                        "rank": float(obs.rank) if obs.rank is not None else 0.0,
                        "mentions_prev": float(obs.mentions_prev or 0),
                    }
                    row.computed_at = utc_now()
                    if existing is None:
                        session.add(row)
                    written += 1

        skipped = len(observations) - written
        log.info(
            "attention: stored %d row(s) for %s%s",
            written,
            session_date,
            f"; {skipped} skipped (symbol not in the tracked universe)" if skipped else "",
        )
        return written

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
            # this does not change what gets fetched for them. The hint is
            # seeded (see ``_social_symbol_hints``) HERE, on the main thread,
            # before the background fetch starts -- the fetch thread itself
            # must never touch the database.
            social_thread, social_box = self._start_social_fetch(
                since=since, until=until, symbols=self._social_symbol_hints(list(symbols))
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

        # Attention runs independently of the post-level sources: it is the
        # one sentiment-adjacent source that still produces data when Reddit
        # is rate-limited, X has no cookie and Stocktwits' watchlist is empty
        # (all three observed in QA), so gating it on ``self.social`` would
        # throw away its main advantage. Keyed to the ET trading session
        # because the API reports a rolling current 24h window with no
        # history endpoint -- today's observation is only ever about today.
        if self.attention:
            self._report_progress("attention", 0, 1)
            self.ingest_attention(current_trading_session(), report)
            self._report_progress("attention", 1, 1)

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
