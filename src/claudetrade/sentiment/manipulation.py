"""Heuristic detection of coordinated or bot-driven social activity.

Every score here is a **heuristic proxy**, not a certified detection of
wrongdoing. The same signals a pump-and-dump group produces -- a burst of
posts, several people saying similar things, concentrated authorship --
are also exactly what a *genuinely* popular stock produces on an active news
day. ``ManipulationDetector`` is deliberately conservative in what it claims
(``reasons`` always spells out *why* a score is elevated) and callers should
treat a high ``manipulation_risk`` as "discount this sample and look closer",
not as proof of manipulation.

**Honest limitations**:

* Near-duplicate detection uses token-set Jaccard over a small window of
  posts; this is O(n^2) and is not intended for windows of many thousands of
  posts (batch/shingle indexing would be needed for that scale).
* Bot-risk signals (account age, karma, followers) are only as good as the
  upstream provider's reporting of them; a provider that does not supply
  these fields makes that component silently uninformative (weighted down,
  not treated as evidence of a bot).
* A single very active but legitimate community account can look like
  "source concentration" identically to a single sockpuppet.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from claudetrade.domain import SocialPost
from claudetrade.sentiment.lexicon import PUMP_DUMP_TEMPLATES, SHORT_SQUEEZE_TERMS
from claudetrade.utils.timeutils import ensure_utc

_WORD_RE_PATTERN = r"[a-z0-9]+"
_NEW_ACCOUNT_DAYS = 30.0
_LOW_KARMA = 20.0
_LOW_FOLLOWERS = 10.0
_BURST_WINDOW_MINUTES = 15.0
_HIGH_POST_RATE_PER_AUTHOR_PER_HOUR = 4.0
_NEAR_DUP_JACCARD_THRESHOLD = 0.75
_COORDINATION_WINDOW_MINUTES = 30.0

# Overall-risk blend weights. Duplication and coordination are weighted
# heaviest because they are the most direct evidence of inauthentic activity;
# bot-risk and pump-language are supporting signals with more false positives.
_WEIGHT_DUPLICATE = 0.30
_WEIGHT_SOURCE_CONCENTRATION = 0.20
_WEIGHT_BOT_RISK = 0.20
_WEIGHT_COORDINATION = 0.20
_WEIGHT_PUMP_PATTERN = 0.10


@dataclass(slots=True)
class ManipulationAssessment:
    """0-1 risk components for one symbol/window's worth of posts."""

    duplicate_ratio: float = 0.0
    source_concentration: float = 0.0
    bot_risk: float = 0.0
    coordination_score: float = 0.0
    pump_pattern_score: float = 0.0
    manipulation_risk: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _tokenise(text: str) -> frozenset[str]:
    import re

    return frozenset(re.findall(_WORD_RE_PATTERN, text.lower()))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _herfindahl(counts: list[int]) -> float:
    total = sum(counts)
    if total <= 0:
        return 0.0
    return sum((c / total) ** 2 for c in counts)


class ManipulationDetector:
    """Computes manipulation-risk heuristics over a set of posts for one
    symbol/window (typically the posts feeding one ``SymbolSentiment``)."""

    def __init__(
        self,
        *,
        near_dup_threshold: float = _NEAR_DUP_JACCARD_THRESHOLD,
        burst_window_minutes: float = _BURST_WINDOW_MINUTES,
        coordination_window_minutes: float = _COORDINATION_WINDOW_MINUTES,
    ):
        self.near_dup_threshold = near_dup_threshold
        self.burst_window_minutes = burst_window_minutes
        self.coordination_window_minutes = coordination_window_minutes

    def assess(
        self, posts: list[SocialPost], *, low_liquidity: bool = False
    ) -> ManipulationAssessment:
        """Assess ``posts`` (already scoped to one symbol and window).

        Args:
            low_liquidity: Caller-supplied hint (e.g. from ``SecurityInfo``
                market cap) that this name is thin. Amplifies
                ``pump_pattern_score`` -- the same template language matters
                more on a name where a handful of buyers can move the price.
        """
        if len(posts) < 2:
            return ManipulationAssessment(reasons=["insufficient posts for manipulation analysis"])

        reasons: list[str] = []

        dup_groups = self._duplicate_groups(posts)
        duplicated = sum(len(g) for g in dup_groups if len(g) > 1)
        duplicate_ratio = duplicated / len(posts)
        if duplicate_ratio > 0.35:
            reasons.append(
                f"{duplicate_ratio:.0%} of posts are exact or near-duplicates of one another"
            )

        author_counts: dict[str, int] = {}
        community_counts: dict[str, int] = {}
        for p in posts:
            author_counts[p.author_hash or "unknown"] = author_counts.get(p.author_hash or "unknown", 0) + 1
            community_counts[p.community or "unknown"] = community_counts.get(p.community or "unknown", 0) + 1
        author_hhi = _herfindahl(list(author_counts.values()))
        community_hhi = _herfindahl(list(community_counts.values()))
        # Either axis being concentrated is a red flag; take the more extreme.
        source_concentration = max(author_hhi, community_hhi)
        if source_concentration > 0.40:
            driver = "authors" if author_hhi >= community_hhi else "communities"
            reasons.append(
                f"posting is concentrated by {driver} (Herfindahl index {source_concentration:.2f})"
            )

        bot_risk = self._bot_risk(posts, reasons)
        coordination_score = self._coordination(posts, dup_groups, reasons)
        pump_pattern_score = self._pump_pattern(posts, low_liquidity, reasons)

        manipulation_risk = (
            _WEIGHT_DUPLICATE * duplicate_ratio
            + _WEIGHT_SOURCE_CONCENTRATION * source_concentration
            + _WEIGHT_BOT_RISK * bot_risk
            + _WEIGHT_COORDINATION * coordination_score
            + _WEIGHT_PUMP_PATTERN * pump_pattern_score
        )
        manipulation_risk = max(0.0, min(1.0, manipulation_risk))

        return ManipulationAssessment(
            duplicate_ratio=duplicate_ratio,
            source_concentration=source_concentration,
            bot_risk=bot_risk,
            coordination_score=coordination_score,
            pump_pattern_score=pump_pattern_score,
            manipulation_risk=manipulation_risk,
            reasons=reasons,
        )

    # -- components -----------------------------------------------------------

    def _duplicate_groups(self, posts: list[SocialPost]) -> list[list[int]]:
        """Group post indices by exact ``text_hash`` first, then near-dup
        (token-set Jaccard) among the remaining singletons.

        O(n^2) on the near-dup pass -- fine for a symbol/session window of a
        few hundred posts, not designed for corpus-scale dedup.
        """
        by_hash: dict[str, list[int]] = {}
        for i, p in enumerate(posts):
            key = p.text_hash or f"__notexthash__{i}"
            by_hash.setdefault(key, []).append(i)

        groups = [g for g in by_hash.values() if len(g) > 1]
        singleton_idx = [i for g in by_hash.values() if len(g) == 1 for i in g]
        tokens = {i: _tokenise(posts[i].text) for i in singleton_idx}

        assigned: set[int] = set()
        for a_pos, i in enumerate(singleton_idx):
            if i in assigned:
                continue
            cluster = [i]
            for j in singleton_idx[a_pos + 1 :]:
                if j in assigned:
                    continue
                if _jaccard(tokens[i], tokens[j]) >= self.near_dup_threshold:
                    cluster.append(j)
            if len(cluster) > 1:
                assigned.update(cluster)
                groups.append(cluster)
        return groups

    def _bot_risk(self, posts: list[SocialPost], reasons: list[str]) -> float:
        known_age = [p.author_age_days for p in posts if p.author_age_days is not None]
        new_account_score = (
            sum(1 for a in known_age if a < _NEW_ACCOUNT_DAYS) / len(known_age) if known_age else 0.0
        )
        if new_account_score > 0.3:
            reasons.append(f"{new_account_score:.0%} of posts are from accounts under 30 days old")

        known_karma = [p.author_karma for p in posts if p.author_karma is not None]
        known_followers = [p.author_followers for p in posts if p.author_followers is not None]
        low_karma_score = (
            sum(1 for k in known_karma if k < _LOW_KARMA) / len(known_karma) if known_karma else 0.0
        )
        low_follower_score = (
            sum(1 for f in known_followers if f < _LOW_FOLLOWERS) / len(known_followers)
            if known_followers
            else 0.0
        )
        low_credibility_score = max(low_karma_score, low_follower_score)

        posting_rate_score = self._posting_rate_score(posts)
        burst_score = self._burst_score(posts)
        if burst_score > 0.5:
            reasons.append("many posts arrived within a very tight time window")

        return max(
            0.0,
            min(
                1.0,
                0.30 * new_account_score
                + 0.25 * low_credibility_score
                + 0.25 * posting_rate_score
                + 0.20 * burst_score,
            ),
        )

    def _posting_rate_score(self, posts: list[SocialPost]) -> float:
        by_author: dict[str, list[dt.datetime]] = {}
        for p in posts:
            by_author.setdefault(p.author_hash or "unknown", []).append(ensure_utc(p.created_at))
        worst = 0.0
        for times in by_author.values():
            if len(times) < 2:
                continue
            span_hours = max(1e-6, (max(times) - min(times)).total_seconds() / 3600.0)
            rate = len(times) / span_hours
            worst = max(worst, rate / _HIGH_POST_RATE_PER_AUTHOR_PER_HOUR)
        return min(1.0, worst)

    def _burst_score(self, posts: list[SocialPost]) -> float:
        """Share of posts that fall inside a tight (``burst_window_minutes``)
        cluster with at least 3 other posts -- a crude burst-timing proxy."""
        times = sorted(ensure_utc(p.created_at) for p in posts)
        if len(times) < 3:
            return 0.0
        window = dt.timedelta(minutes=self.burst_window_minutes)
        in_burst = 0
        for i, t in enumerate(times):
            lo = i
            while lo > 0 and (t - times[lo - 1]) <= window:
                lo -= 1
            hi = i
            while hi < len(times) - 1 and (times[hi + 1] - t) <= window:
                hi += 1
            if hi - lo + 1 >= 4:
                in_burst += 1
        return in_burst / len(times)

    def _coordination(
        self, posts: list[SocialPost], dup_groups: list[list[int]], reasons: list[str]
    ) -> float:
        """Near-identical text from *distinct authors* inside a tight window.

        This is narrower than ``bot_risk``'s generic burst signal: it only
        fires when a duplicate/near-duplicate cluster also spans more than one
        author and a short time span, which is the specific fingerprint of a
        coordinated posting campaign rather than one prolific single poster.
        """
        if not dup_groups:
            return 0.0
        window = dt.timedelta(minutes=self.coordination_window_minutes)
        flagged = 0
        max_cluster = 0
        for group in dup_groups:
            authors = {posts[i].author_hash for i in group}
            if len(authors) < 2:
                continue
            times = sorted(ensure_utc(posts[i].created_at) for i in group)
            if (times[-1] - times[0]) <= window:
                flagged += len(group)
                max_cluster = max(max_cluster, len(group))
        if flagged and max_cluster:
            reasons.append(
                f"{flagged} posts from distinct authors carry near-identical text within "
                f"{self.coordination_window_minutes:.0f} minutes (largest cluster: {max_cluster})"
            )
        return min(1.0, flagged / len(posts))

    def _pump_pattern(
        self, posts: list[SocialPost], low_liquidity: bool, reasons: list[str]
    ) -> float:
        combined = {**PUMP_DUMP_TEMPLATES, **SHORT_SQUEEZE_TERMS}
        hits = 0
        for p in posts:
            text_norm = f" {p.text.lower()} "
            if any(f" {phrase} " in text_norm for phrase in combined):
                hits += 1
        density = hits / len(posts)

        burst = self._burst_score(posts)
        score = density
        if burst > 0.4:
            score *= 1.3
        if low_liquidity:
            score *= 1.2
        score = min(1.0, score)
        if score > 0.4:
            note = "pump-and-dump/squeeze template language is common in this sample"
            if burst > 0.4:
                note += " and arrived in a sudden burst"
            if low_liquidity:
                note += " on a low-liquidity name"
            reasons.append(note)
        return score
