"""Aggregate per-post sentiment into a daily ``SymbolSentiment`` snapshot.

This is the other component (with ``entity_resolution``) where a mistake
propagates everywhere downstream: strategies, signal scoring and the UI all
consume ``SymbolSentiment`` and trust it to be a point-in-time, manipulation-
aware summary. Two invariants matter above all else:

1. **No look-ahead.** A ``SymbolSentiment`` for ``session`` may only be built
   from posts with ``created_at <= session_close_utc(session)``. This is
   asserted, not just filtered, so a bug upstream that leaks a future post
   surfaces immediately as an ``AssertionError`` instead of a silently
   optimistic backtest.
2. **Mention count is not sentiment.** A symbol getting talked about a lot
   says nothing about *which direction* people lean -- that is measured
   separately by ``mention_acceleration`` (attention) versus the various
   polarity measures (direction). Conflating the two is exactly how a stock
   trending because of bad news gets mistaken for a bullish signal. See the
   comment at ``mention_acceleration`` below.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections import defaultdict

from claudetrade.config import SentimentConfig
from claudetrade.domain import (
    SecurityInfo,
    SentimentScores,
    SocialPost,
    SocialSource,
    SymbolSentiment,
    TickerMention,
)
from claudetrade.sentiment.lexicon import FLAIR_CATALYST_TERMS
from claudetrade.sentiment.manipulation import ManipulationDetector
from claudetrade.utils.timeutils import ensure_utc, session_close_utc

log = logging.getLogger(__name__)

#: Below this market cap a name is treated as low-liquidity for the purposes
#: of the manipulation detector's pump-pattern amplification. A rough proxy
#: only -- true liquidity is a market-data concern, not a sentiment one.
_LOW_LIQUIDITY_MARKET_CAP_USD = 300_000_000.0

#: Fallback used by ``_credibility_score`` when called without a config (unit
#: tests exercising the bare function) -- mirrors ``SentimentConfig``'s own
#: default so the two never drift apart silently.
_DEFAULT_CREDIBILITY_BASELINE_BY_SOURCE: dict[str, float] = {
    "news": 0.6,
    "reddit": 0.3,
    "x": 0.3,
}

#: Modest credibility nudge for Reddit's own "DD"/"Due Diligence"/"Analysis"
#: flair (``lexicon.FLAIR_CATALYST_TERMS``) -- the author's own claim to have
#: done research, not verified fact. Kept well under the weight of any one
#: of the three age/karma/follower components below (each contributes at
#: most 1/3 of the computed score), and applied identically whether the
#: post fell into the baseline branch or the fully-computed branch, so it
#: never itself decides which branch runs.
#: Idea (native-field capture): reddit-stock-ai-agent-recommendation (MIT).
_FLAIR_CREDIBILITY_BOOST = 0.1


def time_decay_weight(age_hours: float, half_life_hours: float) -> float:
    """Exponential decay weight: exactly 0.5 when ``age_hours == half_life_hours``.

    Args:
        age_hours: How long before the reference instant (session close) the
            post was created. Must be non-negative -- a negative age means a
            post from *after* the reference instant, which is a look-ahead bug
            further up the pipeline, not something this function should paper
            over silently.
        half_life_hours: Hours after which a post's weight halves.

    Raises:
        ValueError: if ``age_hours`` is negative or ``half_life_hours`` is not
            positive.
    """
    if age_hours < 0:
        raise ValueError(
            f"age_hours must be non-negative (got {age_hours}); a post cannot be newer "
            "than the reference instant without violating no-look-ahead"
        )
    if half_life_hours <= 0:
        raise ValueError(f"half_life_hours must be positive (got {half_life_hours})")
    return math.pow(0.5, age_hours / half_life_hours)


def _credibility_score(post: SocialPost, config: SentimentConfig | None = None) -> float:
    """0-1 proxy for account credibility from age/karma/followers.

    Log-scaled so a single verified/high-follower account cannot single-
    handedly dominate the credibility-weighted average, mirroring the
    engagement log-scaling below.

    "No metrics reported" and "worst possible metrics" are deliberately kept
    distinct: a post whose author fields are ALL ``None`` (e.g. a news-wire
    story, which has no personal author to have karma or a follower count in
    the first place) gets a per-source baseline instead of falling through
    the same ``or 0.0`` floor as a real, karma-less throwaway account. A post
    with *some* metrics present (even a metric explicitly reported as zero)
    is real information and keeps the existing computed score below -- the
    baseline is never blended in for those.
    """
    if post.author_age_days is None and post.author_karma is None and post.author_followers is None:
        baseline_by_source = (
            config.credibility_baseline_by_source
            if config is not None
            else _DEFAULT_CREDIBILITY_BASELINE_BY_SOURCE
        )
        score = baseline_by_source.get(post.source, 0.0)
    else:
        age_component = min(1.0, (post.author_age_days or 0.0) / 365.0)
        karma_component = min(
            1.0, math.log1p(max(0.0, post.author_karma or 0.0)) / math.log1p(10_000.0)
        )
        follower_component = min(
            1.0, math.log1p(max(0.0, post.author_followers or 0.0)) / math.log1p(100_000.0)
        )
        score = max(0.0, min(1.0, (age_component + karma_component + follower_component) / 3.0))

    # Flair prior, applied identically to either branch above (see
    # `_FLAIR_CREDIBILITY_BOOST`).
    if (post.flair or "").strip().casefold() in FLAIR_CATALYST_TERMS:
        score = min(1.0, score + _FLAIR_CREDIBILITY_BOOST)
    return score


def _engagement_weight(post: SocialPost, decay: float) -> float:
    """Decay-scaled engagement weight, log-scaled to avoid one viral post
    dominating the engagement-weighted average.

    ``SocialSource.NEWS`` posts structurally carry no vote/reply counts --
    there is no engagement mechanic on a wire story -- so ``log1p(0) == 0``
    would otherwise give every news post zero weight here, identical to a
    genuinely ignored Reddit/X post. Gating on the source (not on the count
    being zero) gives a news post a neutral, modest-engagement weight
    (``log1p(1.0) == 1.0``) while a Reddit/X post with zero real engagement
    still correctly weighs ~0.
    """
    if post.source == SocialSource.NEWS:
        return decay * 1.0
    return decay * math.log1p(max(0.0, post.engagement))


class SentimentAggregator:
    """Builds ``SymbolSentiment`` snapshots from posts, mentions and scores."""

    def __init__(
        self, config: SentimentConfig, manipulation_detector: ManipulationDetector | None = None
    ):
        self.config = config
        self.manipulation = manipulation_detector or ManipulationDetector()
        #: Accumulated no-look-ahead post drops (see ``aggregate``), flushed
        #: by ``drain_drop_summary()``. Not logged per (symbol, session) pair
        #: any more -- a real refresh across a whole universe and weeks of
        #: sessions produced a near-identical warning line per pair, which
        #: buried everything else in the console; one summary line per run
        #: says the same thing without the noise.
        self._dropped_total = 0
        self._dropped_symbol_days = 0

    def aggregate(
        self,
        symbol: str,
        session: dt.date,
        posts: list[SocialPost],
        mentions: list[TickerMention],
        scores: dict[str, SentimentScores],
        *,
        security: SecurityInfo | None = None,
        source: str = "all",
    ) -> SymbolSentiment:
        """Aggregate one symbol's sentiment as of ``session``'s close.

        Args:
            posts: Candidate posts (any symbol, any date); filtered here to
                those mentioning ``symbol`` at or above
                ``config.min_ticker_confidence`` and dated at or before the
                session close.
            mentions: All resolved mentions across ``posts`` (any symbol);
                filtered here to ``symbol``.
            scores: Per-post ``SentimentScores`` for this symbol, keyed by
                ``SocialPost.external_id``. A post without an entry is skipped
                with a warning (it should have been scored upstream).
            security: Optional reference data, used only to derive the
                low-liquidity hint passed to the manipulation detector.
            source: Label stored on the result (e.g. "reddit", "x", "all").
        """
        close = session_close_utc(session)

        # Invariant: nothing dated after the session close may contribute.
        # Filtering (not raising) is the intended behaviour for routine future
        # -dated noise from clock skew; the assertion right after is the
        # defence against a bug that lets one through anyway.
        eligible_posts = {
            p.external_id: p for p in posts if ensure_utc(p.created_at) <= close
        }
        dropped = len(posts) - len(eligible_posts)
        if dropped:
            self._dropped_total += dropped
            self._dropped_symbol_days += 1
        assert all(ensure_utc(p.created_at) <= close for p in eligible_posts.values()), (
            f"{symbol}/{session}: a post dated after session close survived filtering"
        )

        symbol_mentions = {
            m.post_external_id: m
            for m in mentions
            if m.symbol == symbol and m.confidence >= self.config.min_ticker_confidence
        }
        relevant_posts = [
            eligible_posts[pid] for pid in symbol_mentions if pid in eligible_posts
        ]

        if not relevant_posts:
            return SymbolSentiment(symbol=symbol, session=session, source=source)

        weighted = []
        for post in relevant_posts:
            post_scores = scores.get(post.external_id)
            if post_scores is None:
                log.warning(
                    "%s/%s: no sentiment score for post %s; skipping", symbol, session, post.external_id
                )
                continue
            age_hours = (close - ensure_utc(post.created_at)).total_seconds() / 3600.0
            decay = time_decay_weight(age_hours, self.config.half_life_hours)
            weighted.append((post, post_scores, decay))

        if not weighted:
            return SymbolSentiment(symbol=symbol, session=session, source=source)

        low_liquidity = bool(
            security is not None
            and security.market_cap_usd is not None
            and security.market_cap_usd < _LOW_LIQUIDITY_MARKET_CAP_USD
        )
        manipulation = self.manipulation.assess(relevant_posts, low_liquidity=low_liquidity)

        raw_sentiment = _weighted_mean([(s.polarity, d) for _, s, d in weighted])

        engagement_weights = [_engagement_weight(p, d) for p, _, d in weighted]
        engagement_weighted = _weighted_mean(
            [(s.polarity, w) for (_, s, _), w in zip(weighted, engagement_weights, strict=True)]
        )

        credibility_weights = [d * _credibility_score(p, self.config) for p, _, d in weighted]
        credibility_weighted = _weighted_mean(
            [(s.polarity, w) for (_, s, _), w in zip(weighted, credibility_weights, strict=True)]
        )

        unique_author_sentiment = _unique_author_mean(weighted)

        fast_cutoff = close - dt.timedelta(days=self.config.fast_window_days)
        slow_cutoff = close - dt.timedelta(days=self.config.slow_window_days)
        fast_bucket = [(p, s, d) for p, s, d in weighted if ensure_utc(p.created_at) >= fast_cutoff]
        slow_bucket = [(p, s, d) for p, s, d in weighted if ensure_utc(p.created_at) >= slow_cutoff]
        fast_mean = _weighted_mean([(s.polarity, d) for _, s, d in fast_bucket]) if fast_bucket else 0.0
        slow_mean = _weighted_mean([(s.polarity, d) for _, s, d in slow_bucket]) if slow_bucket else 0.0
        sentiment_acceleration = fast_mean - slow_mean

        # ATTENTION is a separate axis from POLARITY. A symbol getting more
        # mentions tells you people are talking, not what they are saying --
        # counting mentions toward bullishness is exactly the mistake this
        # module exists to avoid. `mention_acceleration` is deliberately
        # computed with no reference to polarity at all.
        fast_days = max(1, self.config.fast_window_days)
        slow_days = max(1, self.config.slow_window_days)
        fast_rate = len(fast_bucket) / fast_days
        slow_rate = len(slow_bucket) / slow_days
        mention_acceleration = (fast_rate - slow_rate) / (slow_rate + 1e-6)
        mention_acceleration = max(-10.0, min(10.0, mention_acceleration))

        bull_sum = sum(s.bullish * d for _, s, d in weighted)
        bear_sum = sum(s.bearish * d for _, s, d in weighted)
        bull_bear_ratio = bull_sum / bear_sum if bear_sum > 1e-9 else (1.0 if bull_sum <= 1e-9 else 10.0)

        dispersion = _weighted_std([(s.polarity, d) for _, s, d in weighted])

        hype = _weighted_mean([(s.hype, d) for _, s, d in weighted])
        fear = _weighted_mean([(s.fear, d) for _, s, d in weighted])
        capitulation = _weighted_mean([(s.capitulation, d) for _, s, d in weighted])
        catalyst_quality = _weighted_mean(
            [
                (max(s.earnings_speculation, s.product_catalyst, s.regulatory_catalyst), d)
                for _, s, d in weighted
            ]
        )

        total_engagement = sum(p.engagement for p, _, _ in weighted)
        unique_authors = len({p.author_hash for p, _, _ in weighted if p.author_hash})
        comment_count = sum(1 for p, _, _ in weighted if p.is_comment)

        avg_age_hours = sum((close - ensure_utc(p.created_at)).total_seconds() / 3600.0 for p, _, _ in weighted) / len(weighted)
        confidence = _aggregate_confidence(
            config=self.config,
            post_count=len(weighted),
            unique_authors=unique_authors,
            duplicate_ratio=manipulation.duplicate_ratio,
            manipulation_risk=manipulation.manipulation_risk,
            dispersion=dispersion,
            avg_age_hours=avg_age_hours,
        )

        labels = {
            "uncertainty": _weighted_mean([(s.uncertainty, d) for _, s, d in weighted]),
            "sarcasm": _weighted_mean([(s.sarcasm, d) for _, s, d in weighted]),
            "fomo": _weighted_mean([(s.fomo, d) for _, s, d in weighted]),
            "rumour": _weighted_mean([(s.rumour, d) for _, s, d in weighted]),
            "short_squeeze": _weighted_mean([(s.short_squeeze, d) for _, s, d in weighted]),
            "pump_and_dump": _weighted_mean([(s.pump_and_dump, d) for _, s, d in weighted]),
            "position_disclosure": _weighted_mean([(s.position_disclosure, d) for _, s, d in weighted]),
            # Options-chatter split (calls vs. puts) -- a per-post signal
            # from `RuleSentimentClassifier`, surfaced the same way as the
            # other labels above; see `SentimentScores.options_call`/
            # `options_put`. Available to strategies via
            # `SymbolSentiment.labels.get("options_call"/"options_put")`.
            "options_call": _weighted_mean([(s.options_call, d) for _, s, d in weighted]),
            "options_put": _weighted_mean([(s.options_put, d) for _, s, d in weighted]),
        }

        return SymbolSentiment(
            symbol=symbol,
            session=session,
            source=source,
            post_count=len(weighted),
            comment_count=comment_count,
            unique_authors=unique_authors,
            raw_sentiment=raw_sentiment,
            engagement_weighted=engagement_weighted,
            credibility_weighted=credibility_weighted,
            unique_author_sentiment=unique_author_sentiment,
            sentiment_acceleration=sentiment_acceleration,
            mention_acceleration=mention_acceleration,
            bull_bear_ratio=bull_bear_ratio,
            dispersion=dispersion,
            source_concentration=manipulation.source_concentration,
            duplicate_ratio=manipulation.duplicate_ratio,
            bot_risk=manipulation.bot_risk,
            manipulation_risk=manipulation.manipulation_risk,
            confidence=confidence,
            hype=hype,
            fear=fear,
            capitulation=capitulation,
            catalyst_quality=catalyst_quality,
            total_engagement=total_engagement,
            labels=labels,
        )

    def drain_drop_summary(self) -> str | None:
        """One human-readable summary of every no-look-ahead post drop
        accumulated across every ``aggregate()`` call since the last drain,
        or ``None`` if nothing was dropped. Resets the counters. The caller
        (``Pipeline.build_sentiment``) logs this once per run instead of a
        line per (symbol, session) pair."""
        if self._dropped_total == 0:
            return None
        total, days = self._dropped_total, self._dropped_symbol_days
        self._dropped_total = 0
        self._dropped_symbol_days = 0
        return (
            f"dropped {total} post(s) dated after their session close across {days} "
            "symbol/session pair(s) this run -- no-look-ahead violation(s) upstream"
        )

    def aggregate_series(
        self,
        symbol: str,
        sessions: list[dt.date],
        posts: list[SocialPost],
        mentions: list[TickerMention],
        scores: dict[str, SentimentScores],
        *,
        security: SecurityInfo | None = None,
        source: str = "all",
    ) -> list[SymbolSentiment]:
        """One ``SymbolSentiment`` per session, each respecting no-look-ahead
        independently (a later session may see posts an earlier one could not,
        never the reverse)."""
        return [
            self.aggregate(
                symbol, session, posts, mentions, scores, security=security, source=source
            )
            for session in sorted(sessions)
        ]


def _weighted_mean(pairs: list[tuple[float, float]]) -> float:
    total_weight = sum(w for _, w in pairs)
    if total_weight <= 1e-12:
        return sum(v for v, _ in pairs) / len(pairs) if pairs else 0.0
    return sum(v * w for v, w in pairs) / total_weight


def _weighted_std(pairs: list[tuple[float, float]]) -> float:
    if len(pairs) < 2:
        return 0.0
    mean = _weighted_mean(pairs)
    total_weight = sum(w for _, w in pairs)
    if total_weight <= 1e-12:
        return 0.0
    variance = sum(w * (v - mean) ** 2 for v, w in pairs) / total_weight
    return math.sqrt(max(0.0, variance))


def _unique_author_mean(
    weighted: list[tuple[SocialPost, SentimentScores, float]],
) -> float:
    """One vote per author: each author's own decay-weighted mean polarity,
    then a plain (unweighted) average across authors.

    This is the manipulation-resistant measure -- ``raw_sentiment`` can be
    moved by one prolific author posting fifty times; this cannot.
    """
    by_author: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for post, scores, decay in weighted:
        key = post.author_hash or f"__anon__{id(post)}"
        by_author[key].append((scores.polarity, decay))
    author_means = [_weighted_mean(pairs) for pairs in by_author.values()]
    return sum(author_means) / len(author_means) if author_means else 0.0


def _aggregate_confidence(
    *,
    config: SentimentConfig,
    post_count: int,
    unique_authors: int,
    duplicate_ratio: float,
    manipulation_risk: float,
    dispersion: float,
    avg_age_hours: float,
) -> float:
    """Explicit multiplicative confidence combination, one factor per cause.

    * ``sample_factor`` -- confidence ramps up toward 1.0 as the sample
      approaches ``min_posts_for_signal``/``min_unique_authors_for_signal``;
      below that, both post count and author count discount it.
    * ``duplicate_factor`` -- a superlinear (squared) discount for duplicate
      content, since a high duplicate ratio means the *effective* sample size
      is much smaller than ``post_count`` suggests.
    * ``manipulation_factor`` -- direct discount from the manipulation-risk
      assessment; a sample that looks coordinated should not be trusted at
      face value regardless of its raw size.
    * ``dispersion_factor`` -- high disagreement among posts (dispersion) is
      itself evidence the "average" is not a stable, meaningful summary.
    * ``staleness_factor`` -- reuses the decay curve (at double the sentiment
      half-life, since staleness is a softer, secondary penalty) applied to
      the *mean* post age, so a sample that is mostly old news is trusted
      less even if it was large and clean when it was fresh.
    """
    sample_factor = min(1.0, post_count / max(1, config.min_posts_for_signal)) * min(
        1.0, unique_authors / max(1, config.min_unique_authors_for_signal)
    )
    duplicate_factor = (1.0 - duplicate_ratio) ** 2
    manipulation_factor = 1.0 - manipulation_risk
    dispersion_factor = max(0.2, 1.0 - min(1.0, dispersion))
    staleness_factor = time_decay_weight(max(0.0, avg_age_hours), config.half_life_hours * 2.0)

    confidence = (
        sample_factor
        * duplicate_factor
        * manipulation_factor
        * dispersion_factor
        * staleness_factor
    )
    return max(0.0, min(1.0, confidence))
