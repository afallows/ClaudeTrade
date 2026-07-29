"""End-to-end orchestration.

Wires the subsystems into the two flows an operator actually runs:

* ``refresh`` -- pull from every configured provider, run the quality checks,
  resolve ticker mentions, classify sentiment and store daily aggregates.
* ``scan``    -- build point-in-time contexts, classify the regime, generate
  ranked signals and write them to the immutable ledger.
* ``backtest`` -- replay the *same* context/scan path over history.

Keeping the wiring in one place is what makes "the backtest and the live scan
run identical code" a checkable claim rather than an aspiration: both call
``ContextBuilder`` and ``SignalEngine.scan``, and there is no second
implementation of either.

Everything degrades rather than fails. A missing social provider, an absent AI
key, or a dead market-data fallback reduces capability and is reported in the
result; it does not abort the run.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from claudetrade.config import AppConfig
from claudetrade.data.context import DatabaseContextProvider
from claudetrade.data.ingest import DataIngestor, IngestReport
from claudetrade.data.quality import QualityReport
from claudetrade.data.snapshot import build_snapshot, persist_snapshot
from claudetrade.data.universe import UniverseSelector
from claudetrade.db.migrations import init_database
from claudetrade.db.models import SymbolSentimentDaily
from claudetrade.db.session import Database
from claudetrade.domain import (
    Bar,
    MarketRegime,
    RegimeState,
    SecurityInfo,
    SentimentScores,
    SocialPost,
    SymbolSentiment,
    TickerMention,
)
from claudetrade.logging_setup import get_logger
from claudetrade.providers.registry import (
    get_ai_provider,
    get_earnings_provider,
    get_market_provider,
    get_social_providers,
    provider_status_report,
)
from claudetrade.regime.market_regime import RegimeClassifier
from claudetrade.sentiment.aggregation import SentimentAggregator
from claudetrade.sentiment.classifiers import RuleSentimentClassifier
from claudetrade.sentiment.entity_resolution import TickerResolver
from claudetrade.signals.engine import ScanResult, SignalEngine
from claudetrade.signals.ledger import SignalLedger
from claudetrade.utils.timeutils import trading_day_range, utc_now

log = get_logger(__name__)


@dataclass(slots=True)
class PipelineResult:
    """Outcome of a refresh or scan, including everything that degraded."""

    started_at: dt.datetime = field(default_factory=utc_now)
    finished_at: dt.datetime | None = None
    ingest: IngestReport | None = None
    scan: ScanResult | None = None
    universe_size: int = 0
    sentiment_rows: int = 0
    snapshot_hash: str = ""
    degraded_sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "universe": self.universe_size,
            "signals": len(self.scan.signals) if self.scan else 0,
            "sentiment_rows": self.sentiment_rows,
            "snapshot": self.snapshot_hash[:12],
            "degraded": self.degraded_sources,
            "warnings": self.warnings,
        }


class Pipeline:
    """Top-level orchestrator used by the CLI, the scheduler and the UI."""

    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self.market = get_market_provider(config)
        self.earnings = get_earnings_provider(config)
        self.social = get_social_providers(config)
        self.ai = get_ai_provider(config)
        self.ledger = SignalLedger(db)
        self.universe = UniverseSelector(config, db)

    @classmethod
    def bootstrap(cls, config: AppConfig) -> Pipeline:
        """Open the database, apply migrations and construct the pipeline."""
        from claudetrade.db.session import get_database

        db = get_database(config)
        init_database(db)
        return cls(config, db)

    # --- provider health ---------------------------------------------------

    def provider_status(self) -> list:
        """Status of every configured provider, for the dashboard."""
        return provider_status_report(self.config)

    # --- refresh -----------------------------------------------------------

    def refresh(
        self,
        *,
        start: dt.date,
        end: dt.date,
        symbols: list[str] | None = None,
    ) -> PipelineResult:
        """Pull, validate and store data from every configured source."""
        result = PipelineResult()

        # Deliberately NOT `as_of=end`: that returns point-in-time membership and
        # would silently drop every company that failed before the end date.
        # Ingestion must capture the full survivorship-unbiased set; it is
        # `UniverseSelector.for_session` that applies point-in-time membership
        # later, per decision session.
        securities = self.market.list_universe()
        if symbols is not None:
            wanted = set(symbols)
            securities = [s for s in securities if s.symbol in wanted]
        # The benchmark is needed for relative strength and the regime model
        # even when it is not itself a candidate.
        benchmark = self.config.market_data.benchmark_symbol
        if benchmark not in {s.symbol for s in securities}:
            securities.append(SecurityInfo(symbol=benchmark, name=benchmark, is_etf=True))

        ingestor = DataIngestor(
            self.config,
            self.db,
            market_provider=self.market,
            earnings_provider=self.earnings,
            social_providers=self.social,
        )
        report = ingestor.run_full_refresh(
            symbols=[s.symbol for s in securities],
            start=start,
            end=end,
            securities=securities,
        )
        result.ingest = report
        result.universe_size = len(securities)
        result.degraded_sources.update(report.provider_failures)

        # run_full_refresh has already fetched social over the same window; do
        # not query the providers a second time.
        posts = report.posts

        if posts:
            directory = {s.symbol: s for s in securities}
            rows = self.build_sentiment(
                posts=posts, directory=directory, start=start, end=end
            )
            result.sentiment_rows = rows
        else:
            result.warnings.append(
                "No social data was ingested; sentiment components will score neutral and "
                "sentiment-dependent strategies will decline."
            )

        result.finished_at = utc_now()
        log.info("refresh complete: %s", result.summary())
        return result

    # --- sentiment ---------------------------------------------------------

    def build_sentiment(
        self,
        *,
        posts: list[SocialPost],
        directory: dict[str, SecurityInfo],
        start: dt.date,
        end: dt.date,
    ) -> int:
        """Resolve mentions, classify posts and store daily aggregates.

        Returns:
            Number of ``symbol_sentiment_daily`` rows written.
        """
        resolver = TickerResolver(directory=directory)
        classifier = RuleSentimentClassifier()
        aggregator = SentimentAggregator(self.config.sentiment)
        threshold = self.config.sentiment.min_ticker_confidence

        # Resolve once; a post mentioning three symbols contributes to three.
        by_symbol: dict[str, list[SocialPost]] = {}
        mentions_by_symbol: dict[str, list[TickerMention]] = {}
        scores_by_symbol: dict[str, dict[str, SentimentScores]] = {}

        for post in posts:
            for mention in resolver.resolve(post):
                if mention.confidence < threshold:
                    # Below-threshold mentions are dropped entirely rather than
                    # down-weighted: an unreliable resolution is noise, and
                    # counting it would let ordinary English inflate attention.
                    continue
                symbol = mention.symbol
                by_symbol.setdefault(symbol, []).append(post)
                mentions_by_symbol.setdefault(symbol, []).append(mention)
                scores_by_symbol.setdefault(symbol, {})[post.external_id] = classifier.classify(
                    post, symbol, [mention]
                )

        sessions = trading_day_range(start, end)
        written = 0
        with self.db.session() as session:
            for symbol, symbol_posts in by_symbol.items():
                for trading_session in sessions:
                    snapshot = aggregator.aggregate(
                        symbol,
                        trading_session,
                        symbol_posts,
                        mentions_by_symbol.get(symbol, []),
                        scores_by_symbol.get(symbol, {}),
                        security=directory.get(symbol),
                    )
                    if snapshot.post_count == 0:
                        continue
                    written += _upsert_sentiment(session, snapshot)
        log.info("stored %d daily sentiment rows across %d symbols", written, len(by_symbol))
        return written

    # --- regime ------------------------------------------------------------

    def classify_regimes(
        self, sessions: list[dt.date], bars_by_symbol: dict[str, list[Bar]]
    ) -> dict[dt.date, RegimeState]:
        """Classify the market environment for each session.

        Falls back to ``UNKNOWN`` rather than guessing when the benchmark is
        absent -- an invented regime would silently move every signal threshold.
        """
        benchmark = self.config.market_data.benchmark_symbol
        benchmark_bars = bars_by_symbol.get(benchmark, [])
        if not benchmark_bars:
            log.warning(
                "benchmark %s unavailable; regime reported as UNKNOWN for all sessions", benchmark
            )
            return {s: RegimeState(session=s, regime=MarketRegime.UNKNOWN) for s in sessions}

        classifier = RegimeClassifier(self.config.regime)
        frame = _bars_to_frame(benchmark_bars)
        # Breadth is computed from the universe's own closes; the classifier
        # takes symbol -> close series and derives the above-50-day fraction.
        breadth_inputs = {
            symbol: _close_series(bars)
            for symbol, bars in bars_by_symbol.items()
            if symbol != benchmark and len(bars) > self.config.regime.trend_slow_ma
        }
        try:
            states = classifier.classify_series(
                benchmark_bars=frame,
                breadth_series=breadth_inputs or None,
            )
        except Exception:
            # Logged with the traceback rather than a bare message: an earlier
            # revision swallowed a signature mismatch here and every session
            # silently reported UNKNOWN, which quietly disabled every
            # regime-dependent adjustment.
            log.exception("regime classification failed; reporting UNKNOWN for all sessions")
            return {s: RegimeState(session=s, regime=MarketRegime.UNKNOWN) for s in sessions}
        return {state.session: state for state in states}

    # --- scan --------------------------------------------------------------

    def scan(
        self,
        session: dt.date,
        *,
        lookback_days: int = 400,
        symbols: list[str] | None = None,
        record: bool = True,
        generate_thesis: bool = True,
    ) -> PipelineResult:
        """Generate ranked signals for one session."""
        result = PipelineResult()
        start = session - dt.timedelta(days=lookback_days)

        universe_report = self.universe.for_session(session)
        candidates = symbols or universe_report.symbols
        result.universe_size = len(candidates)
        if not candidates:
            result.warnings.append(
                "The universe is empty for this session. Run a data refresh first."
            )
            result.finished_at = utc_now()
            return result

        provider = DatabaseContextProvider(
            self.config,
            self.db,
            symbols=candidates,
            start=start,
            end=session,
            quality=QualityReport(),
        )
        bars = provider.bars_by_symbol()
        regimes = self.classify_regimes(provider.sessions() or [session], bars)
        provider._regimes.update(regimes)

        manifest = build_snapshot(
            session=session,
            bars_by_symbol=bars,
            providers={
                "market": getattr(self.market, "name", "unknown"),
                "earnings": getattr(self.earnings, "name", "unknown"),
                "ai": getattr(self.ai, "name", "null"),
            },
            universe_size=len(candidates),
        )
        result.snapshot_hash = persist_snapshot(self.db, manifest)

        contexts = []
        for symbol in provider.symbols_for(session):
            context = provider.build_context(symbol, session)
            if context is not None:
                contexts.append(context)

        engine = SignalEngine(self.config, ai_provider=self.ai)
        scan_result = engine.scan(
            contexts,
            session=session,
            regime=regimes.get(session, RegimeState(session=session, regime=MarketRegime.UNKNOWN)),
            data_snapshot_hash=result.snapshot_hash,
            generate_thesis=generate_thesis,
        )
        result.scan = scan_result
        result.warnings.extend(scan_result.warnings)

        if record:
            for signal in scan_result.signals:
                self.ledger.record(signal)
            # Anything that never triggered inside its window is closed off, so
            # a stale idea cannot be resurrected at a convenient price later.
            self.ledger.expire_stale(session)

        result.finished_at = utc_now()
        log.info("scan complete for %s: %s", session, result.summary())
        return result

    # --- backtest -----------------------------------------------------------

    def make_context_provider(
        self,
        *,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
    ) -> DatabaseContextProvider:
        """Build the provider the backtest engine consumes.

        The regime series is precomputed across the whole window so each
        session's classification uses only its own history.
        """
        provider = DatabaseContextProvider(
            self.config, self.db, symbols=symbols, start=start, end=end
        )
        regimes = self.classify_regimes(provider.sessions(), provider.bars_by_symbol())
        provider._regimes.update(regimes)
        return provider


def _bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
    """Convert domain bars to the OHLCV frame the regime classifier expects."""
    return pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        },
        index=[b.session for b in bars],
    )


def _close_series(bars: list[Bar]) -> pd.Series:
    """Adjusted close series for breadth calculations.

    Breadth compares price to its own moving average, so the adjusted series is
    the right input -- a split would otherwise register as a name falling below
    its average.
    """
    return pd.Series(
        [b.effective_adj_close for b in bars], index=[b.session for b in bars], dtype=float
    )


def _upsert_sentiment(session, snapshot: SymbolSentiment) -> int:
    """Insert or update one daily sentiment row. Returns 1 when written."""
    existing = (
        session.query(SymbolSentimentDaily)
        .filter_by(symbol=snapshot.symbol, session=snapshot.session, source=snapshot.source)
        .one_or_none()
    )
    row = existing or SymbolSentimentDaily(
        symbol=snapshot.symbol, session=snapshot.session, source=snapshot.source
    )
    row.post_count = snapshot.post_count
    row.comment_count = snapshot.comment_count
    row.unique_authors = snapshot.unique_authors
    row.raw_sentiment = snapshot.raw_sentiment
    row.engagement_weighted = snapshot.engagement_weighted
    row.credibility_weighted = snapshot.credibility_weighted
    row.unique_author_sentiment = snapshot.unique_author_sentiment
    row.sentiment_acceleration = snapshot.sentiment_acceleration
    row.mention_acceleration = snapshot.mention_acceleration
    row.bull_bear_ratio = snapshot.bull_bear_ratio
    row.dispersion = snapshot.dispersion
    row.source_concentration = snapshot.source_concentration
    row.duplicate_ratio = snapshot.duplicate_ratio
    row.bot_risk = snapshot.bot_risk
    row.manipulation_risk = snapshot.manipulation_risk
    row.confidence = snapshot.confidence
    row.total_engagement = snapshot.total_engagement
    row.labels = dict(snapshot.labels)
    row.computed_at = utc_now()
    if existing is None:
        session.add(row)
        return 1
    return 0
