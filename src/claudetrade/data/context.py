"""Context construction -- the point-in-time boundary.

This module is where "no look-ahead" stops being a principle and becomes a
mechanism. Every ``StrategyContext`` handed to a strategy is assembled here, and
assembly is the only place data is *truncated to the decision session*:

* bars are sliced to ``session``,
* features are computed from that slice alone,
* sentiment aggregates only posts created at or before the session close,
* earnings entries are filtered by their ``as_of`` knowledge date, so a report
  date that was only announced later is invisible earlier.

``ContextBuilder`` does the work for a single symbol. ``DatabaseContextProvider``
implements the ``ContextProvider`` protocol that ``backtest.engine`` consumes,
so a backtest and a live scan build their contexts through exactly the same
code. If they did not, a discrepancy between them would be undetectable.

Feature computation is the expensive step, so a full feature frame is built once
per symbol and then sliced per session. The frame is built from the *whole*
available history, which is safe only because every indicator is causal --
verified independently by ``features.indicators.assert_causal`` and by the
look-ahead tests.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import select

from claudetrade.config import AppConfig
from claudetrade.data.quality import QualityReport
from claudetrade.db.models import EarningsEventRow, PriceBar, Security, SymbolSentimentDaily
from claudetrade.db.session import Database
from claudetrade.domain import (
    Bar,
    EarningsEvent,
    EarningsSession,
    MarketRegime,
    RegimeState,
    SecurityInfo,
    SymbolSentiment,
)
from claudetrade.features.feature_builder import FeatureBuilder
from claudetrade.logging_setup import get_logger
from claudetrade.strategies.base import StrategyContext, is_attention_only
from claudetrade.utils.timeutils import trading_day_range

log = get_logger(__name__)

#: Minimum bars before a context is worth building at all.
MIN_CONTEXT_BARS = 30


def slice_to_session(frame: pd.DataFrame, session: dt.date) -> pd.DataFrame:
    """Rows of ``frame`` dated at or before ``session``.

    The feature frame may be indexed by ``datetime.date`` or by
    ``pandas.Timestamp`` depending on how it was constructed, and the two do not
    compare against each other. Normalising here keeps that detail out of every
    call site -- and getting it wrong would silently truncate history rather
    than fail loudly.
    """
    if frame.empty:
        return frame
    index = frame.index
    if isinstance(index, pd.DatetimeIndex):
        return frame.loc[index <= pd.Timestamp(session)]
    # Object index holding date-like values.
    mask = [_as_date(value) <= session for value in index]
    return frame.loc[mask]


def _as_date(value: object) -> dt.date:
    """Coerce an index entry to ``datetime.date``."""
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.Timestamp(value).date()


@dataclass(slots=True)
class SymbolData:
    """All history known for one symbol, ahead of any session slicing."""

    symbol: str
    security: SecurityInfo
    bars: list[Bar] = field(default_factory=list)
    earnings: list[EarningsEvent] = field(default_factory=list)
    sentiment: list[SymbolSentiment] = field(default_factory=list)
    features: pd.DataFrame | None = None

    def bars_through(self, session: dt.date) -> list[Bar]:
        return [b for b in self.bars if b.session <= session]


class ContextBuilder:
    """Assembles point-in-time contexts for one symbol at a time."""

    def __init__(
        self,
        config: AppConfig,
        *,
        feature_builder: FeatureBuilder | None = None,
        quality: QualityReport | None = None,
    ):
        self.config = config
        self.features = feature_builder or FeatureBuilder()
        self.quality = quality or QualityReport()

    def build(
        self,
        data: SymbolData,
        session: dt.date,
        *,
        regime: RegimeState,
        benchmark_features: dict[str, float] | None = None,
        sector_features: dict[str, float] | None = None,
    ) -> StrategyContext | None:
        """Build the context for ``session``, or ``None`` when not evaluable.

        Returns ``None`` -- rather than raising -- when the symbol was not
        listed, has been delisted, or has too little history. Those are ordinary
        conditions in a universe that includes failed companies.
        """
        if not data.security.is_active_on(session):
            return None

        bars = data.bars_through(session)
        if len(bars) < MIN_CONTEXT_BARS:
            return None
        if bars[-1].session != session:
            # No bar on the decision session: the name did not trade (halt,
            # holiday mismatch, or a data gap). Acting on a stale price would
            # be a fabricated fill.
            return None

        features = self._features_for(data, session)
        if not features:
            return None

        context = StrategyContext(
            session=session,
            symbol=data.symbol,
            bars=bars,
            features=features,
            security=data.security,
            regime=regime,
            sentiment=self._sentiment_for(data, session),
            sentiment_by_source=self._polarity_by_source(data, session),
            attention_by_source=self._attention_by_source(data, session),
            attention_history=self._attention_history(data, session),
            # COMBINED rows only. This list is what strategies percentile-rank
            # today's snapshot against, and it used to carry every row for the
            # symbol -- per-source breakdowns and aggregator attention rows
            # interleaved with the combined series -- so a value was ranked
            # against a distribution built from other sources entirely. With
            # ApeWisdom rows in the mix that distribution was mostly a
            # different corpus at a different scale.
            sentiment_history=[
                s for s in data.sentiment if s.session <= session and s.source == "all"
            ],
            earnings=self._known_earnings(data, session),
            benchmark_features=dict(benchmark_features or {}),
            sector_features=dict(sector_features or {}),
            data_warnings=self.quality.messages_for(data.symbol),
            config=self.config,
        )
        # Belt and braces: the invariant is re-checked at the boundary as well
        # as inside the engine.
        context.assert_no_lookahead()
        return context

    # --- components -------------------------------------------------------

    def _features_for(self, data: SymbolData, session: dt.date) -> dict[str, float]:
        """Feature dict for ``session``, sliced from the cached frame."""
        if data.features is None or data.features.empty:
            return {}
        subset = slice_to_session(data.features, session)
        if subset.empty:
            return {}
        row = subset.iloc[-1]
        out: dict[str, float] = {}
        for key, value in row.items():
            try:
                fvalue = float(value)
            except (TypeError, ValueError):
                continue
            if fvalue == fvalue:  # exclude NaN
                out[str(key)] = fvalue
        return out

    def _sentiment_for(self, data: SymbolData, session: dt.date) -> SymbolSentiment | None:
        """The COMBINED aggregate for the session, if the sample is usable."""
        return self._polarity_by_source(data, session).get("all")

    def _polarity_by_source(
        self, data: SymbolData, session: dt.date
    ) -> dict[str, SymbolSentiment]:
        """Usable polarity snapshots for ``session``, keyed by source.

        A session can carry several rows -- the combined ``"all"`` aggregate,
        per-source (reddit/x/news/stocktwits) breakdowns, and aggregator
        attention tallies. All three used to be collapsed to one row here and
        that row was then handed to every per-source scoring slot, so one
        sample could stand in as several sources' independent agreement. Each
        source now travels separately and is scored from its own evidence;
        ``signals.scoring`` drops the weight of a source that is absent
        rather than substituting another source's reading.

        Attention rows are excluded outright: they have no polarity to
        contribute (see ``strategies.base.is_attention_only``).

        Each source is gated on sample adequacy INDEPENDENTLY. A per-source
        row is a subset of the combined one, so a source that reported only a
        handful of posts drops out while the combined aggregate it fed can
        still be usable -- which is correct: the thin single-source view is
        not reliable on its own, and it is already represented inside the
        combined row. Omitting a source is meaningfully different from
        storing a zeroed reading for it: absent sentiment is renormalised
        away downstream, never scored as evidence against the candidate.
        """
        cfg = self.config.sentiment
        out: dict[str, SymbolSentiment] = {}
        for snapshot in data.sentiment:
            if snapshot.session != session or is_attention_only(snapshot):
                continue
            if (
                snapshot.post_count < cfg.min_posts_for_signal
                or snapshot.unique_authors < cfg.min_unique_authors_for_signal
            ):
                continue
            out.setdefault(snapshot.source, snapshot)
        return out

    def _attention_by_source(
        self, data: SymbolData, session: dt.date
    ) -> dict[str, SymbolSentiment]:
        """Aggregator attention tallies for ``session``, keyed by source.

        Deliberately NOT gated on ``min_posts_for_signal`` /
        ``min_unique_authors_for_signal``: those measure whether a POLARITY
        sample is large enough to trust, and an attention row reports no
        authors at all (it has none to report), so the author gate would
        reject every row for a reason that does not apply to it. The only
        thing an attention row must have is a mention count.
        """
        return {
            s.source: s
            for s in data.sentiment
            if s.session == session and is_attention_only(s) and s.post_count > 0
        }

    def _attention_history(
        self, data: SymbolData, session: dt.date
    ) -> dict[str, list[SymbolSentiment]]:
        """Each attention source's own trailing series, ascending, through ``session``.

        Per source, never pooled: ApeWisdom's ``all-stocks`` filter watches a
        far larger corpus than its ``4chan`` one, so their mention counts and
        the swings in them are on different scales. Scoring ranks a reading
        against the history of the very source that produced it.
        """
        out: dict[str, list[SymbolSentiment]] = {}
        for snapshot in data.sentiment:
            if snapshot.session > session or not is_attention_only(snapshot):
                continue
            out.setdefault(snapshot.source, []).append(snapshot)
        for series in out.values():
            series.sort(key=lambda s: s.session)
        return out

    def _known_earnings(self, data: SymbolData, session: dt.date) -> list[EarningsEvent]:
        """Earnings entries whose knowledge date is on or before ``session``.

        This is the earnings-date leakage guard. A report scheduled for next
        month is legitimate knowledge *if it had been announced*; if the
        calendar entry only appeared afterwards, it must not be visible.
        """
        visible: list[EarningsEvent] = []
        for event in data.earnings:
            if event.as_of is not None and event.as_of.date() > session:
                continue
            # Actual results are only known after the report itself.
            if event.report_date > session and (
                event.eps_actual is not None or event.surprise_pct is not None
            ):
                visible.append(
                    EarningsEvent(
                        symbol=event.symbol,
                        report_date=event.report_date,
                        session=event.session,
                        confirmed=event.confirmed,
                        eps_estimate=event.eps_estimate,
                        eps_actual=None,
                        revenue_estimate=event.revenue_estimate,
                        revenue_actual=None,
                        surprise_pct=None,
                        source=event.source,
                        as_of=event.as_of,
                    )
                )
                continue
            visible.append(event)
        return visible


class DatabaseContextProvider:
    """``ContextProvider`` implementation backed by the database.

    Loads each symbol's full history once, builds its feature frame once, then
    serves per-session slices. Memory is traded for speed deliberately: a
    backtest over ~120 symbols and ~1,000 sessions would otherwise recompute
    indicators 120,000 times.
    """

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        regimes: dict[dt.date, RegimeState] | None = None,
        quality: QualityReport | None = None,
    ):
        self.config = config
        self.db = db
        self.start = start
        self.end = end
        self._regimes = dict(regimes or {})
        self._builder = ContextBuilder(config, quality=quality)
        self._data: dict[str, SymbolData] = {}
        self._sessions: list[dt.date] = []
        self._benchmark_features: pd.DataFrame | None = None
        self._load(symbols)

    # --- loading -----------------------------------------------------------

    def _load(self, symbols: list[str]) -> None:
        benchmark = self.config.market_data.benchmark_symbol
        wanted = list(dict.fromkeys([*symbols, benchmark]))

        securities = self._load_securities(wanted)
        bars_by_symbol = self._load_bars(wanted)
        earnings_by_symbol = self._load_earnings(wanted)
        sentiment_by_symbol = self._load_sentiment(wanted)

        benchmark_bars = bars_by_symbol.get(benchmark, [])
        if benchmark_bars:
            self._benchmark_features = self._builder.features.build(
                symbol=benchmark, bars=benchmark_bars
            )

        for symbol in wanted:
            bars = bars_by_symbol.get(symbol, [])
            if not bars:
                continue
            data = SymbolData(
                symbol=symbol,
                security=securities.get(symbol, SecurityInfo(symbol=symbol)),
                bars=bars,
                earnings=earnings_by_symbol.get(symbol, []),
                sentiment=sentiment_by_symbol.get(symbol, []),
            )
            try:
                data.features = self._builder.features.build(
                    symbol=symbol,
                    bars=bars,
                    benchmark_bars=benchmark_bars or None,
                )
            except Exception as exc:  # a single bad symbol must not stop a run
                log.warning("feature build failed for %s: %s", symbol, exc)
                continue
            self._data[symbol] = data

        sessions = sorted({b.session for d in self._data.values() for b in d.bars})
        self._sessions = [s for s in sessions if self.start <= s <= self.end]
        log.info(
            "context provider ready: %d symbols, %d sessions (%s to %s)",
            len(self._data),
            len(self._sessions),
            self._sessions[0] if self._sessions else "-",
            self._sessions[-1] if self._sessions else "-",
        )

    def _load_securities(self, symbols: list[str]) -> dict[str, SecurityInfo]:
        with self.db.read_session() as session:
            rows = session.execute(
                select(Security).where(Security.symbol.in_(symbols))
            ).scalars().all()
            return {
                r.symbol: SecurityInfo(
                    symbol=r.symbol,
                    name=r.name,
                    exchange=r.exchange,
                    sector=r.sector,
                    industry=r.industry,
                    market_cap_usd=r.market_cap_usd,
                    shares_outstanding=r.shares_outstanding,
                    is_etf=r.is_etf,
                    is_leveraged_or_inverse=r.is_leveraged_or_inverse,
                    listed_date=r.listed_date,
                    delisted_date=r.delisted_date,
                )
                for r in rows
            }

    def _load_bars(self, symbols: list[str]) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {}
        with self.db.read_session() as session:
            rows = session.execute(
                select(PriceBar)
                .where(PriceBar.symbol.in_(symbols), PriceBar.session <= self.end)
                .order_by(PriceBar.symbol, PriceBar.session)
            ).scalars().all()
            for r in rows:
                out.setdefault(r.symbol, []).append(
                    Bar(
                        symbol=r.symbol,
                        session=r.session,
                        open=r.open,
                        high=r.high,
                        low=r.low,
                        close=r.close,
                        volume=r.volume,
                        adj_close=r.adj_close,
                        source=r.source,
                    )
                )
        return out

    def _load_earnings(self, symbols: list[str]) -> dict[str, list[EarningsEvent]]:
        out: dict[str, list[EarningsEvent]] = {}
        with self.db.read_session() as session:
            rows = session.execute(
                select(EarningsEventRow).where(EarningsEventRow.symbol.in_(symbols))
            ).scalars().all()
            for r in rows:
                out.setdefault(r.symbol, []).append(
                    EarningsEvent(
                        symbol=r.symbol,
                        report_date=r.report_date,
                        session=EarningsSession(r.session)
                        if r.session in {e.value for e in EarningsSession}
                        else EarningsSession.UNKNOWN,
                        confirmed=r.confirmed,
                        eps_estimate=r.eps_estimate,
                        eps_actual=r.eps_actual,
                        revenue_estimate=r.revenue_estimate,
                        revenue_actual=r.revenue_actual,
                        surprise_pct=r.surprise_pct,
                        source=r.source,
                        as_of=r.as_of,
                    )
                )
        return out

    def _load_sentiment(self, symbols: list[str]) -> dict[str, list[SymbolSentiment]]:
        out: dict[str, list[SymbolSentiment]] = {}
        with self.db.read_session() as session:
            rows = session.execute(
                select(SymbolSentimentDaily)
                .where(SymbolSentimentDaily.symbol.in_(symbols))
                .order_by(SymbolSentimentDaily.symbol, SymbolSentimentDaily.session)
            ).scalars().all()
            for r in rows:
                out.setdefault(r.symbol, []).append(
                    SymbolSentiment(
                        symbol=r.symbol,
                        session=r.session,
                        source=r.source,
                        post_count=r.post_count,
                        comment_count=r.comment_count,
                        unique_authors=r.unique_authors,
                        raw_sentiment=r.raw_sentiment,
                        engagement_weighted=r.engagement_weighted,
                        credibility_weighted=r.credibility_weighted,
                        unique_author_sentiment=r.unique_author_sentiment,
                        sentiment_acceleration=r.sentiment_acceleration,
                        mention_acceleration=r.mention_acceleration,
                        bull_bear_ratio=r.bull_bear_ratio,
                        dispersion=r.dispersion,
                        source_concentration=r.source_concentration,
                        duplicate_ratio=r.duplicate_ratio,
                        bot_risk=r.bot_risk,
                        manipulation_risk=r.manipulation_risk,
                        confidence=r.confidence,
                        total_engagement=r.total_engagement,
                        labels=dict(r.labels or {}),
                    )
                )
        return out

    # --- ContextProvider protocol -------------------------------------------

    def sessions(self) -> list[dt.date]:
        return list(self._sessions)

    def symbols_for(self, session: dt.date) -> list[str]:
        """Symbols listed and trading on ``session``.

        Delisted names remain in the provider and are simply absent from the
        sessions after their delisting -- they are never removed from history.
        """
        benchmark = self.config.market_data.benchmark_symbol
        return [
            symbol
            for symbol, data in self._data.items()
            if symbol != benchmark and data.security.is_active_on(session)
        ]

    def build_context(self, symbol: str, session: dt.date) -> StrategyContext | None:
        data = self._data.get(symbol)
        if data is None:
            return None
        return self._builder.build(
            data,
            session,
            regime=self.regime(session),
            benchmark_features=self._benchmark_row(session),
        )

    def bar(self, symbol: str, session: dt.date) -> Bar | None:
        data = self._data.get(symbol)
        if data is None:
            return None
        for candidate in reversed(data.bars):
            if candidate.session == session:
                return candidate
            if candidate.session < session:
                return None
        return None

    def security(self, symbol: str) -> SecurityInfo:
        data = self._data.get(symbol)
        return data.security if data else SecurityInfo(symbol=symbol)

    def regime(self, session: dt.date) -> RegimeState:
        state = self._regimes.get(session)
        if state is not None:
            return state
        # An unclassified session is reported as UNKNOWN rather than assumed
        # benign; the scorer treats it neutrally and confidence is unaffected.
        return RegimeState(session=session, regime=MarketRegime.UNKNOWN)

    # --- helpers --------------------------------------------------------------

    def _benchmark_row(self, session: dt.date) -> dict[str, float]:
        if self._benchmark_features is None or self._benchmark_features.empty:
            return {}
        subset = slice_to_session(self._benchmark_features, session)
        if subset.empty:
            return {}
        row = subset.iloc[-1]
        return {
            str(k): float(v)
            for k, v in row.items()
            if isinstance(v, (int, float)) and float(v) == float(v)
        }

    def bars_by_symbol(self) -> dict[str, list[Bar]]:
        """All loaded bars, used to build a reproducibility snapshot."""
        return {symbol: list(data.bars) for symbol, data in self._data.items()}

    def all_sessions_in_range(self) -> list[dt.date]:
        """Exchange trading days in the configured window.

        Distinct from ``sessions()``, which reflects the sessions for which data
        is actually present. A gap between the two is itself a quality signal.
        """
        return trading_day_range(self.start, self.end)
