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
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from claudetrade.config import AppConfig
from claudetrade.data.context import DatabaseContextProvider
from claudetrade.data.ingest import DataIngestor, IngestReport, _social_fetch_window
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
    SocialSource,
    SymbolSentiment,
    TickerMention,
)
from claudetrade.logging_setup import get_logger
from claudetrade.providers.registry import (
    get_adanos_providers,
    get_ai_provider,
    get_attention_providers,
    get_earnings_provider,
    get_market_provider,
    get_social_providers,
    provider_status_report,
)
from claudetrade.regime.market_regime import RegimeClassifier
from claudetrade.sentiment.aggregation import SentimentAggregator
from claudetrade.sentiment.classifiers import EnsembleSentimentClassifier
from claudetrade.sentiment.entity_resolution import TickerResolver
from claudetrade.sentiment.store import (
    SourceCollection,
    load_stored_posts,
    record_collection_coverage,
    sessions_covered_by_fetch,
)
from claudetrade.signals import funnel_store
from claudetrade.signals.engine import ScanResult, SignalEngine
from claudetrade.signals.ledger import SignalLedger
from claudetrade.utils.timeutils import (
    current_trading_session,
    ensure_utc,
    previous_trading_day,
    session_close_utc,
    session_for_instant,
    trading_day_range,
    utc_now,
)

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
        #: Aggregate mention-count sources (ApeWisdom); separate from
        #: ``social`` because they yield per-symbol tallies, not posts.
        self.attention = get_attention_providers(config)
        #: Adanos (X/Reddit/Polymarket buzz+sentiment); separate from
        #: ``attention`` because its rows carry real polarity and a platform
        #: dimension neither ``social`` nor ``attention``'s storage shape has
        #: room for -- see ``providers.social.adanos``.
        self.adanos = get_adanos_providers(config)
        self.ai = get_ai_provider(config)
        self.ledger = SignalLedger(db)
        self.universe = UniverseSelector(config, db)

    @classmethod
    def bootstrap(cls, config: AppConfig, *, allow_data_fixes: bool = True) -> Pipeline:
        """Open the database, apply migrations and construct the pipeline.

        ``config`` is passed through to ``init_database`` so its data-fix
        hooks (currently the stored-sentiment extraction-version self-heal,
        see ``sentiment.rebuild.ensure_extraction_version``) can actually
        run -- this is the seam every entry point (CLI, web API, MCP server,
        UI) shares.

        Args:
            allow_data_fixes: When ``False``, migrations still run but the
                stored-data self-heal is deferred to another entry point.
                Exists for callers whose start-up latency is part of a
                protocol handshake: the self-heal rebuilds sentiment
                aggregates from every stored post, which is a minute-scale
                job on a real database. That is fine ahead of a command the
                operator just typed, and *not* fine ahead of
                ``mcp_server.run_stdio``'s ``server.run()`` -- an MCP client
                that launches the server as a subprocess (Claude Desktop)
                would sit through the whole rebuild before the first tool
                call and can give up on the handshake first. The heal is
                deferred, never skipped: the stamp is only written once a
                rebuild succeeds, so the next CLI/UI bootstrap still does it.
        """
        from claudetrade.db.session import get_database

        db = get_database(config)
        init_database(db, config, allow_data_fixes=allow_data_fixes)
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
        social_lookback_hours: int | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> PipelineResult:
        """Pull, validate and store data from every configured source.

        Args:
            social_lookback_hours: When given, social sources fetch only this
                many hours back (up to now) instead of the full ``start``--
                ``end`` calendar window. Lets the price-history window be
                sized for context building (90+ days) without asking social
                providers for months of posts they cannot supply anyway.
            progress_callback: Optional ``(phase, done, total)`` hook, see
                ``data.ingest.DataIngestor.progress_callback`` -- used by the
                web API's background-refresh endpoint
                (``webapi.routers.system``) to expose live progress; the CLI
                does not pass one and is unaffected.
        """
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
            attention_providers=self.attention,
            adanos_providers=self.adanos,
            progress_callback=progress_callback,
        )
        report = ingestor.run_full_refresh(
            symbols=[s.symbol for s in securities],
            start=start,
            end=end,
            securities=securities,
            social_lookback_hours=social_lookback_hours,
        )
        result.ingest = report
        result.universe_size = len(securities)
        result.degraded_sources.update(report.provider_failures)

        # run_full_refresh has already fetched social over the same window; do
        # not query the providers a second time.
        posts = report.posts

        # Coverage is recorded whether or not anything was fetched, and
        # BEFORE the early-return below: "the collector ran and found
        # nothing" is a confirmed zero and must be written down, while "the
        # collector was down" must not be mistaken for one. Recording only
        # on the happy path would make an outage indistinguishable from
        # silence, which is the whole failure this table exists to end.
        self._record_collection_coverage(
            report, start=start, end=end, social_lookback_hours=social_lookback_hours
        )

        if posts:
            directory = {s.symbol: s for s in securities}
            rows = self.build_sentiment(
                posts=posts,
                directory=directory,
                history=self._stored_history_for(posts),
                start=start,
                end=end,
                progress_callback=progress_callback,
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

    def _stored_history_for(self, fresh: list[SocialPost]) -> list[SocialPost]:
        """Persisted posts this run's sessions need as trailing context.

        This is the read that makes the baseline *rolling*. Without it
        ``build_sentiment`` only ever sees one fetch's worth of posts (~72
        hours, all the providers will serve), so every daily aggregate is
        built from that single fetch and no amount of running the
        application accumulates a longer history -- the defect that turned
        1,830 posts into 14 rows.

        The window is bounded twice over, because an unbounded read of
        ``social_posts`` is the largest scan in the database:

        * Forward: the earliest session this run can rewrite is the session
          its OLDEST fresh post belongs to (nothing earlier is in scope --
          see ``build_sentiment``), and aggregating that session honestly
          needs the posts leading up to it. ``sentiment.lookback_days`` is
          that trailing context, the same padding ``rebuild_sentiment``
          uses.
        * Backward: ``sentiment.history_window_days`` is the hard floor, so
          a one-off backfill covering a year of price history still cannot
          make one refresh read the entire post table.
        """
        if not fresh:
            return []
        earliest_session = min(
            session_for_instant(ensure_utc(p.created_at)) for p in fresh
        )
        context_start = session_close_utc(earliest_session) - dt.timedelta(
            days=max(0, self.config.sentiment.lookback_days)
        )
        floor = utc_now() - dt.timedelta(
            days=max(1, self.config.sentiment.history_window_days)
        )
        return load_stored_posts(self.db, since=max(context_start, floor))

    def _record_collection_coverage(
        self,
        report: IngestReport,
        *,
        start: dt.date,
        end: dt.date,
        social_lookback_hours: int | None,
    ) -> None:
        """Write down which sessions this run's collection actually covered.

        Best-effort: a coverage write must never take a refresh down. Losing
        one run's coverage row degrades a baseline denominator; raising here
        would lose the refresh that produced the data in the first place.
        """
        if not self.social and not self.attention:
            return
        try:
            now = utc_now()
            # The ingestor's own window helper, imported rather than
            # recomputed: coverage claiming a window the fetch did not
            # actually cover is worse than no coverage at all, and two
            # copies of this arithmetic would drift apart silently.
            since, until = _social_fetch_window(start, end, social_lookback_hours)
            # `until` may sit in the future (the calendar-window path ends at
            # end-of-day); a session cannot have been collected before it has
            # closed, so the claim is clamped to now.
            until = min(ensure_utc(until), now) if until is not None else now

            if self.social:
                outcomes = [
                    _source_outcome(provider, report.provider_failures)
                    for provider in self.social
                ]
                record_collection_coverage(
                    self.db,
                    sessions=sessions_covered_by_fetch(since, until),
                    outcomes=outcomes,
                    posts=report.posts,
                )
            if self.attention:
                # Attention sources report a rolling current-24h tally with
                # no history endpoint, so `ingest_attention` keys them to the
                # current session and only that session is theirs to claim.
                record_collection_coverage(
                    self.db,
                    sessions=[current_trading_session(now)],
                    outcomes=[
                        _source_outcome(provider, report.provider_failures)
                        for provider in self.attention
                    ],
                )
        except Exception:
            log.exception(
                "recording social collection coverage failed; this refresh's data is "
                "unaffected but the sessions it covered will read as untracked"
            )

    # --- sentiment ---------------------------------------------------------

    def build_sentiment(
        self,
        *,
        posts: list[SocialPost],
        directory: dict[str, SecurityInfo],
        start: dt.date,
        end: dt.date,
        history: list[SocialPost] | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> int:
        """Resolve mentions, classify posts and store daily aggregates.

        ``posts`` is the DRIVING set -- what this run just learned -- and it
        alone decides which (symbol, session) pairs are recomputed.
        ``history`` is persisted context (``sentiment.store.
        load_stored_posts``): it feeds every aggregate so a session's numbers
        reflect the whole rolling window rather than one ~72-hour fetch, but
        it never widens the write scope. Keeping those two roles apart is
        what makes the rolling baseline possible without turning every
        refresh into a whole-universe rebuild: a refresh recomputes a few
        sessions of a few hundred symbols, not 2,400 symbols x 90 sessions.

        ``rebuild_sentiment`` passes everything as ``posts`` and no
        ``history``, which is how it still rebuilds its full window -- the
        maintenance path's scope is deliberately unbounded, the refresh
        path's deliberately is not.

        A row is written for a (symbol, session) pair only when at least one
        DRIVING post for the symbol actually falls inside that session's own
        window -- after the previous session's close, at or before its own.
        Two failure modes this prevents, both observed live:

        * **Fabricated freshness.** Aggregating one static post set for every
          session in the range produced a current-session row byte-identical
          to yesterday's whenever nothing new had been fetched yet (weighted
          means are invariant to the uniform extra decay between two closes).
          A session that gained no posts now simply has no row, which
          downstream correctly reads as "no fresh sentiment", rather than a
          silently duplicated one.
        * **History rewrites.** A later refresh whose providers only look
          back ~72 hours used to recompute (and overwrite) every session in
          the whole refresh window from just the posts it happened to
          re-fetch, degrading historical rows originally built from richer
          live data. Sessions outside this fetch's real post coverage are now
          left untouched.

        Each written row aggregates the full decayed post set as of its
        session close -- driving posts UNION persisted history, deduped by
        ``(source, external_id)`` with the fresh copy winning because it may
        carry updated engagement counts. Only *which* sessions get written is
        bounded by driving-post coverage.

        Transaction scope (QA handoff v3, F26): one short write transaction
        PER SYMBOL, with all aggregation compute done before it opens. This
        loop used to hold a single write transaction across the entire
        symbol x session pass -- minutes on a real universe -- which (a) held
        SQLite's write lock so every other process's writes queued behind it
        for the whole duration, and (b) deferred WAL checkpointing (which
        only runs at commit) so the WAL ballooned and slowed every
        cross-process *read* too. Upserts are idempotent, so a run killed
        mid-loop just re-covers the same ground next time.

        Args:
            history: Persisted posts to aggregate alongside ``posts``,
                contributing evidence but never scope. ``None`` reproduces
                the pre-rolling-baseline behaviour exactly.
            progress_callback: Optional ``(phase, done, total)`` hook, the
                same shape as ``data.ingest.ProgressCallback``. Reports
                ``("sentiment_aggregate", symbols_done, symbols_total)`` per
                symbol -- this is what keeps the cross-process refresh
                heartbeat (``db.refresh_state_store``) alive through a long
                aggregation pass that the ingest phases' own callbacks no
                longer cover. Exceptions from it are swallowed, matching
                ``DataIngestor._report_progress``.

        Returns:
            Number of ``symbol_sentiment_daily`` rows written.
        """
        resolver = TickerResolver(directory=directory)
        # The ensemble with no AI classifier attached is the rule classifier
        # plus one thing the rule classifier cannot do: fold in the author's
        # own bull/bear tag (`SocialPost.sentiment_prior`), which is a fact
        # about the post rather than a feature of its text. Constructed
        # without an `ai_classifier` deliberately -- per-post LLM calls across
        # a 90-day window are not something a refresh should incur silently --
        # so for every post that carries no tag this is byte-for-byte the
        # previous behaviour, and no stored aggregate changes meaning.
        classifier = EnsembleSentimentClassifier()
        aggregator = SentimentAggregator(self.config.sentiment)
        threshold = self.config.sentiment.min_ticker_confidence

        # Resolve once; a post mentioning three symbols contributes to three.
        # Mentions are re-derived here even for stored posts rather than read
        # from ``ticker_mentions``: the refresh and the rebuild must agree
        # symbol-for-symbol on identical stored input, and that table is
        # cleared wholesale by a rebuild (see ``sentiment.store``).
        by_symbol: dict[str, list[SocialPost]] = {}
        mentions_by_symbol: dict[str, list[TickerMention]] = {}
        scores_by_symbol: dict[str, dict[str, SentimentScores]] = {}
        #: symbol -> instants of the DRIVING posts only. This is the scope
        #: key: history contributes to the numbers, never to which rows get
        #: rewritten, or a refresh would recompute the entire stored window
        #: from whatever subset of it the providers happened to re-serve.
        driving_times: dict[str, list[dt.datetime]] = {}

        def _absorb(post: SocialPost, symbol: str, mention: TickerMention) -> None:
            by_symbol.setdefault(symbol, []).append(post)
            mentions_by_symbol.setdefault(symbol, []).append(mention)
            scores_by_symbol.setdefault(symbol, {})[post.external_id] = classifier.classify(
                post, symbol, [mention]
            )

        for post in posts:
            for mention in resolver.resolve(post):
                if mention.confidence < threshold:
                    # Below-threshold mentions are dropped entirely rather than
                    # down-weighted: an unreliable resolution is noise, and
                    # counting it would let ordinary English inflate attention.
                    continue
                _absorb(post, mention.symbol, mention)
                driving_times.setdefault(mention.symbol, []).append(
                    ensure_utc(post.created_at)
                )

        # History is absorbed only for symbols this run is actually
        # recomputing. Classifying every stored post against every symbol it
        # happens to name would pay for hundreds of symbols whose rows this
        # refresh will not touch -- the cost that makes a rolling read
        # affordable per refresh instead of a nightly job.
        driving_keys = {(p.source.value, p.external_id) for p in posts}
        history_used = 0
        for post in history or []:
            if (post.source.value, post.external_id) in driving_keys:
                continue  # superseded: the fresh copy has current engagement
            absorbed = False
            for mention in resolver.resolve(post):
                if mention.confidence < threshold or mention.symbol not in driving_times:
                    continue
                _absorb(post, mention.symbol, mention)
                absorbed = True
            history_used += int(absorbed)

        # The session range must reach far enough forward to *hold* every
        # driving post, or posts silently belong to nothing. Refreshes run
        # after the close (the owner's ran at 22:13 ET), so most gathered
        # posts are dated after the last session's close -- they were falling
        # outside every window and vanishing: 1,830 posts produced 14 rows,
        # with the remainder reported as "look-ahead violations" they never
        # were. Such a post is early information about the NEXT session,
        # which is exactly what ``session_for_instant`` returns; attributing
        # it to the session that already closed would be the real look-ahead.
        # History deliberately cannot extend this horizon: a stale stored
        # post must not conjure a row for a session this run learned nothing
        # about.
        all_driving_times = [t for times in driving_times.values() for t in times]
        last_session = end
        first_session = start
        if all_driving_times:
            last_session = max(last_session, session_for_instant(max(all_driving_times)))
            # Sessions before the oldest driving post cannot change, so they
            # are never even considered -- this is what keeps a refresh
            # incremental instead of a whole-window rebuild.
            first_session = max(start, session_for_instant(min(all_driving_times)))

        sessions = trading_day_range(first_session, last_session)
        # Per-session freshness window: (previous session's close, own close].
        session_windows: list[tuple[dt.date, dt.datetime, dt.datetime]] = []
        for trading_session in sessions:
            lower = session_close_utc(previous_trading_day(trading_session))
            session_windows.append(
                (trading_session, lower, session_close_utc(trading_session))
            )

        written = 0
        total_symbols = len(driving_times)
        for done, (symbol, post_times) in enumerate(driving_times.items(), start=1):
            symbol_posts = by_symbol.get(symbol, [])
            # All compute happens BEFORE the write transaction opens -- the
            # transaction below holds the write lock only for this symbol's
            # upserts, never for aggregation work.
            snapshots = []
            for trading_session, lower, upper in session_windows:
                if not any(lower < t <= upper for t in post_times):
                    continue  # no fresh post for this session -- see docstring
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
                snapshots.append(snapshot)
            if snapshots:
                with self.db.session() as session:
                    for snapshot in snapshots:
                        written += _upsert_sentiment(session, snapshot)
            if progress_callback is not None:
                try:
                    progress_callback("sentiment_aggregate", done, total_symbols)
                except Exception:
                    log.debug("progress callback raised; ignored", exc_info=True)
        drop_summary = aggregator.drain_drop_summary()
        if drop_summary:
            log.warning(drop_summary)
        log.info(
            "stored %d daily sentiment rows across %d symbols (%d fresh post(s) plus "
            "%d contributing from stored history)",
            written,
            total_symbols,
            len(posts),
            history_used,
        )
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
        """Generate ranked signals for one session.

        When ``session`` has no stored price bars -- the normal intraday case
        (daily bars land after the close), a weekend/holiday date, or simply
        a database that has not been refreshed today -- the scan falls back
        to the most recent stored session within the last 7 calendar days and
        says so in ``result.warnings``, instead of silently evaluating zero
        symbols. A scanner that answers "no data yet, here is the latest
        complete session's read" is useful; one that returns an empty list
        with no explanation is indistinguishable from a broken one.
        """
        result = PipelineResult()

        latest_bar_session = self._latest_bar_session(upto=session)
        if latest_bar_session is None:
            result.warnings.append(
                f"No price bars are stored at or before {session}. "
                "Run a data refresh before scanning."
            )
            result.finished_at = utc_now()
            return result
        if latest_bar_session != session:
            if (session - latest_bar_session).days > 7:
                result.warnings.append(
                    f"No price bars stored for {session}; the latest stored session "
                    f"{latest_bar_session} is more than 7 days old. Refusing to scan "
                    "stale data -- run a data refresh."
                )
                result.finished_at = utc_now()
                return result
            result.warnings.append(
                f"No price bars stored for {session} (data not yet ingested for that "
                f"session); scanning the latest stored session {latest_bar_session} "
                "instead. Run a refresh to evaluate the current session."
            )
            session = latest_bar_session

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
                "ai": getattr(self.ai, "name", "none"),
            },
            universe_size=len(candidates),
        )
        result.snapshot_hash = persist_snapshot(self.db, manifest)

        contexts = []
        for symbol in provider.symbols_for(session):
            context = provider.build_context(symbol, session)
            if context is not None:
                contexts.append(context)
        if not contexts:
            # The engine's own "no candidate cleared the thresholds" warning
            # only fires when at least one symbol was evaluated; zero contexts
            # would otherwise return a completely silent empty scan.
            result.warnings.append(
                f"No symbol produced an evaluable context for {session} "
                f"(each needs 30+ bars of history ending exactly on that session). "
                "The stored data is likely incomplete -- run a data refresh."
            )

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
        # Cross-process diagnosability (see `signals.funnel_store`'s module
        # docstring): the CLI, the web API server and the MCP server each
        # bootstrap their own `Pipeline`, so a rejection funnel that only
        # lived on this in-memory `scan_result` would be invisible to "why no
        # picks today?" asked from a different one of those processes.
        # Best-effort -- never raises. A degenerate zero-context scan writes
        # nothing so it cannot clobber the previous, informative funnel.
        if scan_result.evaluated_symbols:
            funnel_store.save(self.config, scan_result)

        if record:
            for signal in scan_result.signals:
                # Same-session re-scans are now routine (the intraday
                # fallback above targets the latest stored session every
                # time), and a signal's id is deterministic per (symbol,
                # strategy, session, config, code) while its content embeds
                # ``created_at`` -- so re-recording an id that already exists
                # would always trip the immutability check. First write wins:
                # an already-recorded id is an idempotent re-scan, not an
                # error.
                if self.ledger.get(signal.signal_id, verify=False) is not None:
                    continue
                # One unrecordable signal (a true same-id/different-content
                # collision indicates corruption at this point, since
                # already-recorded ids were skipped above) must not abort
                # recording the rest of the batch.
                outcome = self.ledger.record_or_report(signal)
                if not outcome.ok:
                    scan_result.record_errors[signal.signal_id] = outcome.error or "unknown"
                    result.warnings.append(
                        f"signal {signal.signal_id} could not be recorded: {outcome.error}"
                    )
            # Anything that never triggered inside its window is closed off, so
            # a stale idea cannot be resurrected at a convenient price later.
            self.ledger.expire_stale(session)

        result.finished_at = utc_now()
        log.info("scan complete for %s: %s", session, result.summary())
        return result

    def _latest_bar_session(self, *, upto: dt.date) -> dt.date | None:
        """Most recent session at or before ``upto`` with stored price bars."""
        from sqlalchemy import func, select

        from claudetrade.db.models import PriceBar

        with self.db.session() as db_session:
            return db_session.execute(
                select(func.max(PriceBar.session)).where(PriceBar.session <= upto)
            ).scalar()

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

    # --- social-only collection ---------------------------------------------

    def collect_social(
        self,
        *,
        lookback_hours: int,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> PipelineResult:
        """Social posts and aggregator attention ONLY -- never the market pass.

        The narrow entry point ``claudetrade.scheduler`` runs every hour, and
        the reason it must be narrow is cost asymmetry: a full ``refresh``
        spends ~20 rate-limit-bound minutes on per-symbol price/earnings
        fetches that change once a day, while the data that is genuinely
        perishable -- Reddit ``/new``, X recent-search, ApeWisdom's rolling
        24h snapshot -- is cheap and unrecoverable once its window rolls past.
        Running the whole refresh hourly would burn the market provider's rate
        budget for nothing; running nothing hourly loses baseline forever.

        Structurally, not just conventionally, market-free: the
        ``DataIngestor`` built here is given ``market_provider=None`` and
        ``earnings_provider=None``, so no code path from this method can
        reach a price fetch even if it were changed to call one.

        The symbol directory (needed to resolve mentions and to gate attention
        rows to tracked tickers) comes from ``UniverseSelector.load_all`` --
        stored securities, falling back to the packaged seed lists -- never
        from ``market.list_universe()``, which for the live providers is a
        network call and would reintroduce exactly the cost this method
        exists to avoid.

        Aggregation reuses ``build_sentiment`` unchanged, over this
        collection's own posts, exactly as ``refresh`` does; the session
        window is bounded by ``lookback_hours`` so an hourly run rewrites only
        the sessions its posts actually cover and leaves older history alone.

        Returns:
            A ``PipelineResult`` whose ``ingest`` report carries the post and
            mention counts, plus ``sentiment_rows`` and any degraded sources.
        """
        # Local import: this is the only method that needs the ET session
        # helper, and keeping it out of the module import block keeps this
        # addition to a single self-contained block.
        from claudetrade.utils.timeutils import current_trading_session

        result = PipelineResult()
        report = IngestReport()
        result.ingest = report

        if not self.social and not self.attention and not self.adanos:
            result.warnings.append(
                "No social or attention provider is configured; there is nothing to collect. "
                "Sentiment history cannot accumulate until at least one is enabled."
            )
            result.finished_at = utc_now()
            return result

        securities = self.universe.load_all()
        directory = {s.symbol: s for s in securities}
        result.universe_size = len(directory)
        if not directory:
            result.warnings.append(
                "No securities are known, so ticker mentions cannot be resolved. "
                "Run 'claudetrade refresh' once to populate the universe."
            )

        ingestor = DataIngestor(
            self.config,
            self.db,
            market_provider=None,
            earnings_provider=None,
            social_providers=self.social,
            attention_providers=self.attention,
            adanos_providers=self.adanos,
            progress_callback=progress_callback,
        )

        def _report(phase: str, done: int) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(phase, done, 3)
            except Exception:
                log.debug("progress callback raised; ignored", exc_info=True)

        end = current_trading_session()
        since = utc_now() - dt.timedelta(hours=max(1, lookback_hours))
        start = since.date()

        posts: list[SocialPost] = []
        if self.social:
            _report("social", 0)
            posts = ingestor.ingest_social(since=since, until=None, report=report)
            if directory:
                ingestor.resolve_and_persist_mentions(posts, directory, report)

        if posts and directory:
            _report("sentiment", 1)
            result.sentiment_rows = self.build_sentiment(
                posts=posts,
                directory=directory,
                start=start,
                end=end,
                progress_callback=progress_callback,
            )
        elif self.social and not posts:
            result.warnings.append(
                "No social posts were returned in this window; sentiment aggregates are "
                "unchanged for this collection."
            )

        if self.attention:
            _report("attention", 2)
            # Keyed to the ET trading session for the same reason
            # ``run_full_refresh`` does: the API reports a rolling current-24h
            # window with no history endpoint, so today's observation is only
            # ever about today.
            ingestor.ingest_attention(end, report)

        if self.adanos:
            # Same independence rationale as the attention block above --
            # Adanos is not gated on the post-level sources being healthy.
            ingestor.ingest_adanos(end, report)

        _report("finishing", 3)
        result.degraded_sources.update(report.provider_failures)
        report.finished_at = utc_now()
        result.finished_at = utc_now()
        log.info(
            "social collection complete: %d post(s) fetched, %d new, %d mention(s), "
            "%d sentiment row(s)%s",
            len(posts),
            report.posts_inserted,
            report.mentions_inserted,
            result.sentiment_rows,
            f", degraded: {sorted(result.degraded_sources)}" if result.degraded_sources else "",
        )
        return result


def _source_outcome(provider: object, failures: dict[str, str]) -> SourceCollection:
    """One provider's coverage outcome for this run.

    Labelled by the ``SocialSource`` the provider emits rather than by the
    adapter's own name, so coverage rows line up with the ``source`` column
    in ``symbol_sentiment_daily`` and an operator reading both sees the same
    vocabulary. Attention providers have no ``SocialSource`` (they yield
    tallies, not posts) and fall back to their name, which is the same stem
    their aggregate rows use (``apewisdom:*``).

    A provider absent from ``failures`` succeeded -- including when it
    returned nothing, which is the confirmed-zero case this whole table
    exists to make visible.
    """
    name = str(getattr(provider, "name", "social"))
    source = getattr(provider, "source", None)
    label = source.value if isinstance(source, SocialSource) else name
    error = failures.get(name, "")
    return SourceCollection(
        source=label, ok=not error, error=f"{name}: {error}"[:300] if error else ""
    )


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
