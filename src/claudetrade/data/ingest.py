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
from dataclasses import dataclass, field

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
from claudetrade.utils.timeutils import ensure_utc, utc_now

log = get_logger(__name__)


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
    ):
        self.config = config
        self.db = db
        self.market = market_provider
        self.earnings = earnings_provider
        self.social = list(social_providers or [])
        self.checker = DataQualityChecker(config, db)

    # --- reference data ---------------------------------------------------

    def ingest_securities(self, securities: list[SecurityInfo], report: IngestReport) -> None:
        """Upsert reference data and alias rows."""
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

        batch_size = max(1, self.config.market_data.max_symbols_per_request)
        collected: dict[str, list[Bar]] = {}
        for i in range(0, len(symbols), batch_size):
            chunk = symbols[i : i + batch_size]
            try:
                fetched = self.market.get_daily_bars(chunk, start, end)
            except ProviderError as exc:
                log.error("market provider failed for %d symbols: %s", len(chunk), exc)
                report.provider_failures[getattr(self.market, "name", "market")] = str(exc)
                continue
            collected.update(fetched)

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
        """Fetch posts from every enabled social provider.

        A provider that is not configured is skipped silently -- that is the
        documented reduced-capability path, not an error.
        """
        report = report or IngestReport()
        posts: list[SocialPost] = []
        for provider in self.social:
            try:
                fetched = provider.fetch_posts(since=since, until=until, symbols=symbols)
            except ProviderError as exc:
                name = getattr(provider, "name", "social")
                log.warning("social provider %s unavailable: %s", name, exc)
                report.provider_failures[name] = str(exc)
                continue
            posts.extend(fetched)

        if posts:
            self._persist_posts(posts, report)
            report.posts.extend(posts)
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
        """One complete refresh cycle across every configured source."""
        report = IngestReport()
        reference = {s.symbol: s for s in (securities or [])}
        if securities:
            self.ingest_securities(securities, report)
        self.ingest_prices(symbols, start, end, report, securities=reference)
        self.ingest_corporate_actions(symbols, start, end, report)
        self.ingest_earnings(symbols, start, end, report)

        if self.social:
            # Backfill social over the same window as the price history when a
            # lookback is not given explicitly, so a historical refresh produces
            # sentiment covering the same period as its bars.
            if social_lookback_hours is not None:
                since = utc_now() - dt.timedelta(hours=social_lookback_hours)
                until = None
            else:
                since = dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC)
                until = dt.datetime.combine(end, dt.time.max, tzinfo=dt.UTC)
            self.ingest_social(since=since, until=until, symbols=symbols, report=report)

        self.checker.persist(report.quality)
        report.finished_at = utc_now()
        log.info("ingestion complete: %s", report.summary())
        return report


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
