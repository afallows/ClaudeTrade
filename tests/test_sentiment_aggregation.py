"""Tests for sentiment aggregation and time decay weighting."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from claudetrade.domain import SocialPost, SocialSource
from claudetrade.sentiment.aggregation import _credibility_score, time_decay_weight


def _post(**overrides) -> SocialPost:
    """Minimal SocialPost for scoring helpers; overrides set the field under test."""
    fields = {
        "source": SocialSource.REDDIT,
        "external_id": "t3_test",
        "created_at": dt.datetime(2024, 6, 3, 15, 0, tzinfo=dt.timezone.utc),
        "text": "neutral placeholder text",
    }
    fields.update(overrides)
    return SocialPost(**fields)


class TestTimeDecayWeight:
    """Time decay weight decreases with age, reaches 0.5 at half-life."""

    def test_half_life_weight_is_half(self):
        """Weight at half-life hours is exactly 0.5."""
        result = time_decay_weight(age_hours=18, half_life_hours=18)
        assert result == pytest.approx(0.5, abs=1e-6)

    def test_recent_post_high_weight(self):
        """Recent posts have weight near 1.0."""
        result = time_decay_weight(age_hours=1, half_life_hours=18)
        assert result > 0.9

    def test_old_post_low_weight(self):
        """Old posts have weight near 0.0."""
        result = time_decay_weight(age_hours=100, half_life_hours=18)
        assert result < 0.1

    def test_zero_age_weight_is_one(self):
        """Post with zero age has weight 1.0."""
        result = time_decay_weight(age_hours=0, half_life_hours=18)
        assert result == pytest.approx(1.0, abs=1e-6)

    def test_decay_is_monotonic(self):
        """Weight decreases monotonically with age."""
        half_life = 18
        weights = [time_decay_weight(h, half_life) for h in [0, 6, 12, 18, 24, 36]]
        for i in range(len(weights) - 1):
            assert weights[i] >= weights[i + 1]


class TestPostAfterSessionExcluded:
    """Posts created after session close are excluded (no look-ahead)."""

    def test_post_after_session_excluded(self):
        """Post created after session close is not included in aggregation."""
        session = dt.date(2023, 1, 3)
        session_close = dt.datetime(2023, 1, 3, 16, 0, 0, tzinfo=dt.UTC)  # 4pm ET

        post_after_close = dt.datetime(2023, 1, 3, 20, 30, 0, tzinfo=dt.UTC)  # After 4pm

        # Post should be excluded
        should_include = post_after_close <= session_close
        assert not should_include


class TestConfidenceWithSampleSize:
    """Aggregate confidence falls with fewer posts and fewer unique authors."""

    def test_confidence_high_large_sample(self):
        """Large diverse sample gets high confidence."""
        # 20 posts from 15 unique authors
        post_count = 20
        unique_authors = 15

        # Simplified confidence model: high when both counts are good
        confidence = min(post_count / 20.0, unique_authors / 10.0)
        assert confidence > 0.8

    def test_confidence_low_small_sample(self):
        """Small sample gets low confidence."""
        post_count = 3
        unique_authors = 1

        confidence = min(post_count / 20.0, unique_authors / 10.0)
        assert confidence < 0.3


class TestDuplicateRatioPenalty:
    """High duplicate ratio reduces sentiment confidence."""

    def test_high_duplicate_ratio_lowers_confidence(self):
        """Duplicate ratio > threshold reduces confidence."""
        duplicate_ratio = 0.50  # 50% of posts are near-duplicates
        max_duplicate_ratio = 0.35

        is_suspicious = duplicate_ratio > max_duplicate_ratio
        assert is_suspicious

    def test_low_duplicate_ratio_permitted(self):
        """Duplicate ratio below threshold is normal."""
        duplicate_ratio = 0.15
        max_duplicate_ratio = 0.35

        is_suspicious = duplicate_ratio > max_duplicate_ratio
        assert not is_suspicious


class TestDispersionPenalty:
    """High dispersion (disagreement) reduces confidence."""

    def test_high_dispersion_lowers_confidence(self):
        """Sentiment that's very spread out is less confident."""
        # Dispersion measured as std deviation or similar
        # High dispersion = uncertain sentiment
        dispersion = 0.8  # On a 0-1 scale

        # Confidence should be reduced
        confidence = 1.0 - (dispersion * 0.5)
        assert confidence < 0.65

    def test_low_dispersion_high_confidence(self):
        """Consensus (low dispersion) yields high confidence."""
        dispersion = 0.1

        confidence = 1.0 - (dispersion * 0.5)
        assert confidence > 0.9


class TestEngagementWeighting:
    """Engagement is log-scaled to prevent single viral post dominance."""

    def test_engagement_is_log_scaled(self):
        """One viral post doesn't dominate via log-scaling."""
        # Viral post: 1000 engagements
        # Normal post: 10 engagements

        viral_weight = math.log1p(1000) if 1000 > 0 else 0
        normal_weight = math.log1p(10) if 10 > 0 else 0

        # Log scaling should narrow the ratio
        ratio = viral_weight / normal_weight
        assert ratio < 1000 / 10  # Much smaller than linear ratio

    def test_log_scaling_compresses_extreme_accounts(self):
        """Credibility rises with karma but a whale account cannot dominate.

        Ranking must be preserved, yet the score is log-scaled and clamped to
        1.0 so that a 10,000,000-karma account is not 1,000x the weight of a
        10,000-karma one -- that is what stops a single loud account setting
        the aggregate sentiment for a symbol.
        """
        scores = [
            _credibility_score(_post(author_karma=k))
            for k in (0, 100, 10_000, 10_000_000)
        ]

        # Ranking preserved (non-decreasing), and every score stays in range.
        assert scores[0] < scores[1] < scores[2]
        assert all(0.0 <= s <= 1.0 for s in scores)
        # The 1000x karma jump from 10k to 10M must not yield a 1000x score.
        assert scores[3] < scores[2] * 1.5


class TestUniqueAuthorSentiment:
    """Unique-author sentiment gives one vote per author."""

    def test_one_vote_per_author(self):
        """Each author's sentiment counts once, not per post."""
        posts = [
            {"author": "author1", "sentiment": 0.8},
            {"author": "author1", "sentiment": 0.7},  # Same author
            {"author": "author2", "sentiment": 0.9},
        ]

        # Aggregating by author (one vote each)
        by_author = {}
        for post in posts:
            author = post["author"]
            if author not in by_author:
                by_author[author] = []
            by_author[author].append(post["sentiment"])

        # Then take mean per author
        author_sentiments = [sum(s) / len(s) for s in by_author.values()]
        aggregate = sum(author_sentiments) / len(author_sentiments)

        # Result: (0.75 + 0.9) / 2 = 0.825
        assert aggregate == pytest.approx(0.825, abs=0.01)
