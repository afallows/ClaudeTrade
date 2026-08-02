"""Per-symbol sentiment and mention history, and what is rising in it.

This application's premise is that a stock worth looking at announces
itself as *rising* attention and *improving* tone before it announces
itself in price. That premise needs a time series per symbol, and a way to
ask "what changed" across the universe. ``symbol_sentiment_daily`` has
stored the raw material all along -- one row per (symbol, session, source),
carrying ``post_count`` (mentions) and the sentiment aggregates -- but
nothing read it as a series, so nothing could see a trend.

**Storage stays sparse; readers densify.** A row exists only for a session
that actually gained data. Writing a zero row for every symbol on every
session would be ~216,000 rows per quarter on a 2,400-symbol universe,
almost all of them zeros, and would slow every query to record the absence
of news. Absence is not missing data here -- "no posts that session" *is*
zero mentions -- so :func:`symbol_series` fills the gaps at read time and
the stored table stays small.

**Sources are kept apart, never blended.** ``"all"`` is this installation's
own post-derived aggregate: it carries real polarity, from text this app
resolved and classified itself. ``apewisdom:*`` rows carry attention volume
only, across a far wider corpus, and no direction whatsoever (see
``providers.social.apewisdom``). Summing them would invent a bull/bear
reading for mentions nobody ever read, so mention counts combine while
sentiment is only ever reported from the sources that measure it.

**Everything is as-of.** Every query is bounded by a session and reads only
sessions at or before it, so a history call inside a backtest cannot see
its own future.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select

from claudetrade.db.models import Security, SymbolSentimentDaily
from claudetrade.db.session import Database
from claudetrade.logging_setup import get_logger
from claudetrade.utils.timeutils import trading_day_range

log = get_logger(__name__)

#: The post-derived combined aggregate -- the only source with polarity.
LOCAL_SOURCE = "all"

#: Prefix of the aggregator attention rows (``apewisdom:4chan``, ...).
ATTENTION_PREFIX = "apewisdom:"

#: Below this many mentions in the recent window, a "surge" is noise: going
#: from one mention to three is a 200% rise and means nothing. Ranking on
#: ratios without a floor surfaces exactly that garbage.
DEFAULT_MIN_RECENT_MENTIONS = 5


@dataclass(slots=True)
class HistoryPoint:
    """One symbol, one session. ``observed`` marks a real stored row."""

    session: dt.date
    mentions: int = 0
    attention_mentions: int = 0
    sentiment: float = 0.0
    bull_bear_ratio: float | None = None
    confidence: float = 0.0
    unique_authors: int = 0
    observed: bool = False

    @property
    def total_mentions(self) -> int:
        """Local post mentions plus aggregator mentions.

        Both count "times this symbol was talked about", over different
        corpora, so they add. Sentiment deliberately does not -- see the
        module docstring.
        """
        return self.mentions + self.attention_mentions

    def to_dict(self) -> dict[str, object]:
        return {
            "session": self.session.isoformat(),
            "mentions": self.mentions,
            "attention_mentions": self.attention_mentions,
            "total_mentions": self.total_mentions,
            "sentiment": round(self.sentiment, 4),
            "bull_bear_ratio": (
                round(self.bull_bear_ratio, 4) if self.bull_bear_ratio is not None else None
            ),
            "confidence": round(self.confidence, 4),
            "unique_authors": self.unique_authors,
            "observed": self.observed,
        }


@dataclass(slots=True)
class SymbolTrend:
    """A symbol's recent activity measured against its own baseline.

    Self-relative on purpose. Comparing a symbol's mentions to the
    universe's would just rediscover that NVDA is discussed more than a
    small-cap; comparing it to its OWN normal is what makes a quiet name
    waking up visible at all -- and that is the event this app exists to
    catch.
    """

    symbol: str
    company_name: str = ""
    recent_mentions: int = 0
    baseline_rate: float = 0.0
    recent_rate: float = 0.0
    mention_change: float = 0.0
    recent_sentiment: float | None = None
    baseline_sentiment: float | None = None
    sentiment_change: float | None = None
    sessions_observed: int = 0
    has_polarity: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "recent_mentions": self.recent_mentions,
            "recent_rate_per_session": round(self.recent_rate, 3),
            "baseline_rate_per_session": round(self.baseline_rate, 3),
            "mention_change": round(self.mention_change, 4),
            "recent_sentiment": (
                round(self.recent_sentiment, 4) if self.recent_sentiment is not None else None
            ),
            "baseline_sentiment": (
                round(self.baseline_sentiment, 4)
                if self.baseline_sentiment is not None
                else None
            ),
            "sentiment_change": (
                round(self.sentiment_change, 4) if self.sentiment_change is not None else None
            ),
            "sessions_observed": self.sessions_observed,
            "has_polarity": self.has_polarity,
        }


@dataclass(slots=True)
class HistoryWindow:
    """A symbol's densified series plus the summary a caller usually wants."""

    symbol: str
    start: dt.date
    end: dt.date
    points: list[HistoryPoint] = field(default_factory=list)

    @property
    def total_mentions(self) -> int:
        return sum(p.total_mentions for p in self.points)

    @property
    def sessions_with_activity(self) -> int:
        return sum(1 for p in self.points if p.total_mentions > 0)

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "sessions": len(self.points),
            "sessions_with_activity": self.sessions_with_activity,
            "total_mentions": self.total_mentions,
            "points": [p.to_dict() for p in self.points],
        }


def symbol_series(
    db: Database,
    symbol: str,
    *,
    as_of: dt.date,
    days: int = 90,
) -> HistoryWindow:
    """One symbol's daily mention/sentiment series, gap-filled with zeros.

    Trading sessions only: a series padded with weekends would report a
    two-day mention drought every week and make any rate calculation wrong.
    Sessions with no stored row come back as real zeros with
    ``observed=False``, so a caller can tell "nobody mentioned it" from
    "we have no data for that day" while still doing arithmetic over a
    continuous series.
    """
    symbol = symbol.strip().upper()
    sessions = trading_day_range(as_of - dt.timedelta(days=days), as_of)
    if not sessions:
        return HistoryWindow(symbol=symbol, start=as_of, end=as_of)

    with db.read_session() as session:
        rows = session.execute(
            select(SymbolSentimentDaily)
            .where(
                SymbolSentimentDaily.symbol == symbol,
                SymbolSentimentDaily.session >= sessions[0],
                SymbolSentimentDaily.session <= as_of,
            )
            .order_by(SymbolSentimentDaily.session)
        ).scalars().all()

    by_session: dict[dt.date, HistoryPoint] = {
        s: HistoryPoint(session=s) for s in sessions
    }
    for row in rows:
        point = by_session.get(row.session)
        if point is None:
            continue  # a stored non-trading-day row; not part of the series
        point.observed = True
        if row.source == LOCAL_SOURCE:
            point.mentions += row.post_count
            point.sentiment = row.raw_sentiment
            point.bull_bear_ratio = row.bull_bear_ratio
            point.confidence = row.confidence
            point.unique_authors = row.unique_authors
        elif row.source.startswith(ATTENTION_PREFIX):
            # Per-community rows sum: a symbol named on both /biz/ and the
            # equity subreddits was mentioned by both populations.
            point.attention_mentions += row.post_count
        # Per-source local breakdowns ("reddit", "news", ...) are skipped:
        # they are already inside the "all" aggregate and would double-count.

    return HistoryWindow(
        symbol=symbol,
        start=sessions[0],
        end=sessions[-1],
        points=[by_session[s] for s in sessions],
    )


def rising_symbols(
    db: Database,
    *,
    as_of: dt.date,
    recent_sessions: int = 3,
    baseline_sessions: int = 20,
    limit: int = 25,
    min_recent_mentions: int = DEFAULT_MIN_RECENT_MENTIONS,
) -> list[SymbolTrend]:
    """Symbols whose chatter is accelerating against their own baseline.

    The screen this application exists to run. ``recent_sessions`` of
    activity are compared to the ``baseline_sessions`` immediately before
    them -- as *rates per session*, so unequal window lengths compare
    honestly -- and symbols are ranked by proportional change.

    Deliberate choices, each load-bearing:

    * **Rates, not totals.** A 3-session recent window against a 20-session
      baseline compared by total would call every symbol a collapse.
    * **A mention floor.** ``min_recent_mentions`` keeps 1 -> 3 mentions off
      the top of the list; unfloored ratios rank noise first, every time.
    * **A zero baseline is a real surge, capped.** A symbol nobody discussed
      that starts being discussed is the single most interesting case here,
      so it is kept -- with change clipped to the same +/-10 band
      ``sentiment.aggregation`` uses, rather than an infinity that would
      make sorting meaningless.
    * **Sentiment is reported, never ranked on.** Attention is what rises
      first; tone qualifies it. A symbol whose mentions tripled while tone
      turned sharply negative is a different trade (see the hype-failure
      strategy), not a filtered-out one -- so it stays in the list with its
      negative ``sentiment_change`` visible.

    Only symbols present in ``securities`` are considered, the same guard
    ``mcp_server.get_trending`` applies -- an aggregator naming a ticker
    this installation does not track is not evidence about anything it
    screens.
    """
    total_sessions = max(1, recent_sessions) + max(1, baseline_sessions)
    # Calendar padding: N trading sessions span roughly 1.45x that in
    # calendar days once weekends and holidays are counted.
    sessions = trading_day_range(
        as_of - dt.timedelta(days=int(total_sessions * 1.6) + 10), as_of
    )
    if len(sessions) < 2:
        return []
    sessions = sessions[-total_sessions:]
    recent_window = set(sessions[-recent_sessions:])
    baseline_window = set(sessions[:-recent_sessions])
    if not baseline_window:
        return []

    with db.read_session() as db_session:
        rows = db_session.execute(
            select(
                SymbolSentimentDaily.symbol,
                SymbolSentimentDaily.session,
                SymbolSentimentDaily.source,
                SymbolSentimentDaily.post_count,
                SymbolSentimentDaily.raw_sentiment,
            )
            .join(Security, Security.symbol == SymbolSentimentDaily.symbol)
            .where(
                SymbolSentimentDaily.session >= sessions[0],
                SymbolSentimentDaily.session <= as_of,
            )
        ).all()
        names = dict(
            db_session.execute(select(Security.symbol, Security.name)).all()
        )

    #: symbol -> (recent_mentions, baseline_mentions, recent polarity samples,
    #: baseline polarity samples, sessions seen)
    agg: dict[str, dict] = {}
    for symbol, session_date, source, post_count, raw_sentiment in rows:
        if source != LOCAL_SOURCE and not source.startswith(ATTENTION_PREFIX):
            continue  # per-source local breakdown; already inside "all"
        bucket = agg.setdefault(
            symbol,
            {"recent": 0, "baseline": 0, "recent_pol": [], "baseline_pol": [], "seen": set()},
        )
        bucket["seen"].add(session_date)
        in_recent = session_date in recent_window
        bucket["recent" if in_recent else "baseline"] += post_count or 0
        # Polarity only from the source that actually measures it, and only
        # when the session carried posts -- a zero-mention row's 0.0
        # sentiment is "unmeasured", not "neutral", and averaging it in
        # would drag every real reading toward zero.
        if source == LOCAL_SOURCE and post_count:
            bucket["recent_pol" if in_recent else "baseline_pol"].append(raw_sentiment)

    recent_n = max(1, len(recent_window))
    baseline_n = max(1, len(baseline_window))

    trends: list[SymbolTrend] = []
    for symbol, bucket in agg.items():
        if bucket["recent"] < min_recent_mentions:
            continue
        recent_rate = bucket["recent"] / recent_n
        baseline_rate = bucket["baseline"] / baseline_n
        if baseline_rate <= 0:
            # Silence -> chatter. Real, and the most interesting case; the
            # cap keeps it sortable instead of infinite.
            change = 10.0
        else:
            change = max(-10.0, min(10.0, (recent_rate - baseline_rate) / baseline_rate))

        recent_pol = bucket["recent_pol"]
        baseline_pol = bucket["baseline_pol"]
        recent_sentiment = sum(recent_pol) / len(recent_pol) if recent_pol else None
        baseline_sentiment = (
            sum(baseline_pol) / len(baseline_pol) if baseline_pol else None
        )
        sentiment_change = (
            recent_sentiment - baseline_sentiment
            if recent_sentiment is not None and baseline_sentiment is not None
            else None
        )

        trends.append(
            SymbolTrend(
                symbol=symbol,
                company_name=names.get(symbol) or "",
                recent_mentions=bucket["recent"],
                baseline_rate=baseline_rate,
                recent_rate=recent_rate,
                mention_change=change,
                recent_sentiment=recent_sentiment,
                baseline_sentiment=baseline_sentiment,
                sentiment_change=sentiment_change,
                sessions_observed=len(bucket["seen"]),
                has_polarity=bool(recent_pol),
            )
        )

    # Ties on a capped change (every from-silence symbol sits at 10.0) break
    # on raw recent volume, so the loudest genuine surge leads.
    trends.sort(key=lambda t: (t.mention_change, t.recent_mentions), reverse=True)
    return trends[: max(1, limit)]


def coverage_summary(db: Database, *, as_of: dt.date, days: int = 90) -> dict[str, object]:
    """How much history actually exists -- the honest 'can I trust a trend?'.

    Trend detection needs a baseline, and a database three sessions old
    cannot supply one no matter how good the code is. Reporting coverage
    alongside the trends is what keeps an empty or warming-up installation
    from reading as "nothing is rising".
    """
    start = as_of - dt.timedelta(days=days)
    with db.read_session() as session:
        distinct_sessions = session.execute(
            select(func.count(func.distinct(SymbolSentimentDaily.session))).where(
                SymbolSentimentDaily.session >= start,
                SymbolSentimentDaily.session <= as_of,
            )
        ).scalar() or 0
        symbols = session.execute(
            select(func.count(func.distinct(SymbolSentimentDaily.symbol))).where(
                SymbolSentimentDaily.session >= start,
                SymbolSentimentDaily.session <= as_of,
            )
        ).scalar() or 0
        earliest, latest = session.execute(
            select(
                func.min(SymbolSentimentDaily.session),
                func.max(SymbolSentimentDaily.session),
            ).where(SymbolSentimentDaily.session <= as_of)
        ).one()
        with_attention = session.execute(
            select(func.count(func.distinct(SymbolSentimentDaily.symbol))).where(
                SymbolSentimentDaily.source.like(f"{ATTENTION_PREFIX}%"),
                SymbolSentimentDaily.session >= start,
                SymbolSentimentDaily.session <= as_of,
            )
        ).scalar() or 0

    return {
        "window_days": days,
        "sessions_with_data": int(distinct_sessions),
        "symbols_with_history": int(symbols),
        "symbols_with_attention_data": int(with_attention),
        "earliest_session": earliest.isoformat() if earliest else None,
        "latest_session": latest.isoformat() if latest else None,
    }


__all__ = [
    "ATTENTION_PREFIX",
    "LOCAL_SOURCE",
    "HistoryPoint",
    "HistoryWindow",
    "SymbolTrend",
    "coverage_summary",
    "rising_symbols",
    "symbol_series",
]
