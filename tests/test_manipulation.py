"""Tests for sentiment manipulation and coordination detection."""

from __future__ import annotations

import datetime as dt

from claudetrade.domain import SocialPost, SocialSource
from claudetrade.sentiment.manipulation import (
    ManipulationDetector,
)


def make_post(
    text: str,
    author_hash: str = "author1",
    created_at: dt.datetime | None = None,
    score: int = 10,
) -> SocialPost:
    """Helper to create posts."""
    if created_at is None:
        created_at = dt.datetime(2023, 1, 15, 12, 0, tzinfo=dt.UTC)

    return SocialPost(
        source=SocialSource.REDDIT,
        external_id=f"post_{hash(text)}",
        created_at=created_at,
        text=text,
        author_hash=author_hash,
        score=score,
    )


class TestNearIdenticalTextCoordination:
    """Near-identical text from different authors in tight window signals coordination."""

    def test_identical_text_different_authors_high_risk(self):
        """Same text from 3 different authors within 1 hour."""
        now = dt.datetime(2023, 1, 15, 12, 0, tzinfo=dt.UTC)
        one_hour_ago = now - dt.timedelta(hours=1)

        posts = [
            make_post("Buy $TEST at market open", author_hash="author1", created_at=one_hour_ago),
            make_post(
                "Buy $TEST at market open",
                author_hash="author2",
                created_at=one_hour_ago + dt.timedelta(minutes=15),
            ),
            make_post(
                "Buy $TEST at market open",
                author_hash="author3",
                created_at=one_hour_ago + dt.timedelta(minutes=30),
            ),
        ]

        # High coordination risk
        # Similar texts, different authors, tight time window
        detector = ManipulationDetector()
        assessment = detector.assess(posts)
        assert assessment.coordination_score > 0.2 or assessment.duplicate_ratio > 0.5


class TestSingleAuthorRepetition:
    """Single author posting repeatedly raises source concentration risk."""

    def test_high_source_concentration_single_author(self):
        """One author posting 8 of 10 posts."""
        posts = [make_post(f"Text {i}", author_hash="author1") for i in range(8)]
        posts += [make_post(f"Text {i}", author_hash=f"author{i + 2}") for i in range(2)]

        detector = ManipulationDetector()
        assessment = detector.assess(posts)
        # 8 posts from one author out of 10 should show high source concentration
        assert assessment.source_concentration > 0.5


class TestOrganicDiscussion:
    """Organic varied discussion scores LOW on manipulation."""

    def test_organic_low_coordination_risk(self):
        """Diverse authors, varied times and texts."""
        posts = []
        for i in range(5):
            post = make_post(
                f"Interesting analysis point {i}",
                author_hash=f"author{i}",
                created_at=dt.datetime(2023, 1, 15, 12, i * 30, tzinfo=dt.UTC),
            )
            posts.append(post)

        detector = ManipulationDetector()
        assessment = detector.assess(posts)
        # Organic discussion should have low risk
        assert assessment.manipulation_risk < 0.4


class TestDuplicateDetection:
    """Duplicate posts are detected and ratio computed."""

    def test_exact_duplicate_detection(self):
        """Identical text is flagged as duplicate."""
        text = "Great stock pick, definitely buying in"
        posts = [
            make_post(
                text,
                author_hash="author1",
                created_at=dt.datetime(2023, 1, 15, 12, 0, tzinfo=dt.UTC),
            ),
            make_post(
                text,
                author_hash="author2",
                created_at=dt.datetime(2023, 1, 15, 12, 5, tzinfo=dt.UTC),
            ),
            make_post("Different opinion here", author_hash="author3"),
        ]

        detector = ManipulationDetector()
        assessment = detector.assess(posts)
        # 2 duplicates out of 3 posts should show duplicate ratio
        assert assessment.duplicate_ratio > 0.2

    def test_near_duplicate_detection(self):
        """Nearly identical text is flagged as duplicate."""
        posts = [
            make_post("$TEST is the best trade ever!", author_hash="author1"),
            make_post("$TEST is the best trade ever", author_hash="author2"),  # Minor punctuation
            make_post("$TEST is a great opportunity", author_hash="author3"),
        ]

        detector = ManipulationDetector()
        assessment = detector.assess(posts)
        # First two are near-duplicates
        assert assessment.duplicate_ratio > 0.1


class TestTightTimeWindow:
    """Coordination requires tight time window (e.g., 1 hour)."""

    def test_same_text_wide_time_spread_low_risk(self):
        """Same text hours apart is less suspicious."""
        posts = [
            make_post(
                "Buy $TEST",
                author_hash="author1",
                created_at=dt.datetime(2023, 1, 15, 10, 0, tzinfo=dt.UTC),
            ),
            make_post(
                "Buy $TEST",
                author_hash="author2",
                created_at=dt.datetime(2023, 1, 15, 14, 0, tzinfo=dt.UTC),
            ),
        ]

        detector = ManipulationDetector()
        assessment = detector.assess(posts)
        # 4 hours apart => outside 1-hour window, low coordination
        assert assessment.coordination_score < 0.3

    def test_same_text_within_hour_high_risk(self):
        """Same text within 1 hour from different authors."""
        posts = [
            make_post(
                "Buy $TEST",
                author_hash="author1",
                created_at=dt.datetime(2023, 1, 15, 12, 0, tzinfo=dt.UTC),
            ),
            make_post(
                "Buy $TEST",
                author_hash="author2",
                created_at=dt.datetime(2023, 1, 15, 12, 30, tzinfo=dt.UTC),
            ),
        ]

        detector = ManipulationDetector()
        assessment = detector.assess(posts)
        # Within 1 hour => higher risk of coordination
        assert assessment.manipulation_risk > 0.1


class TestMultiAuthorSametime:
    """Multiple distinct authors posting the same content in a short window."""

    def test_multiple_authors_same_message_coordination(self):
        """3+ authors posting same message within minutes."""
        base_time = dt.datetime(2023, 1, 15, 12, 0, tzinfo=dt.UTC)
        posts = [
            make_post("$TEST is undervalued", author_hash="author1", created_at=base_time),
            make_post(
                "$TEST is undervalued",
                author_hash="author2",
                created_at=base_time + dt.timedelta(minutes=1),
            ),
            make_post(
                "$TEST is undervalued",
                author_hash="author3",
                created_at=base_time + dt.timedelta(minutes=2),
            ),
        ]

        detector = ManipulationDetector()
        assessment = detector.assess(posts)
        assert assessment.duplicate_ratio > 0.5 or assessment.coordination_score > 0.3


class TestFalsePositivePrevention:
    """Avoid false positives from natural discussion."""

    def test_common_technical_phrase_not_spam(self):
        """Technical terms used independently don't flag manipulation."""
        posts = [
            make_post("$TEST showing bullish divergence on daily", author_hash="author1"),
            make_post("$TEST has a bullish divergence", author_hash="author2"),
            make_post("$TEST divergence is bullish", author_hash="author3"),
        ]

        # While text is similar (technical term), authors are different
        # and phrasing varies, so manipulation risk should be low
        detector = ManipulationDetector()
        assessment = detector.assess(posts)
        assert assessment.manipulation_risk < 0.5


class TestViralPost:
    """Viral reposts/quotes by many users on the same post."""

    def test_viral_post_many_engagements(self):
        """Single post with 1000+ interactions from many users."""
        original_post = make_post(
            "$TEST merger announcement!",
            author_hash="news_source",
            score=1000,
        )

        # Many users reposting (in a real system, these would be comment/quote posts)
        # Repost engagement should NOT be flagged as manipulation of the source post
        # But concentrated engagement on one post might signal attention, not deception

        assert original_post.engagement == 1000


class TestManipulationScoring:
    """Overall manipulation risk combines multiple signals."""

    def test_high_risk_combines_signals(self):
        """High duplicate ratio + high concentration + tight window = high risk."""
        base_time = dt.datetime(2023, 1, 15, 12, 0, tzinfo=dt.UTC)

        # Many identical posts from same author
        posts = [
            make_post(
                "Pump $TEST!", author_hash="spammer", created_at=base_time + dt.timedelta(minutes=i)
            )
            for i in range(5)
        ]

        detector = ManipulationDetector()
        assessment = detector.assess(posts)

        # All signals should be elevated for obvious spam
        assert assessment.duplicate_ratio > 0.5
        assert assessment.source_concentration > 0.5
        # Combined risk should be high
        assert assessment.manipulation_risk > 0.3

    def test_low_risk_organic(self):
        """Low risk combines for organic posts."""
        base_time = dt.datetime(2023, 1, 15, 12, 0, tzinfo=dt.UTC)

        posts = [
            make_post(
                f"$TEST analysis {i}: different angle",
                author_hash=f"author{i}",
                created_at=base_time + dt.timedelta(hours=i),
            )
            for i in range(3)
        ]

        detector = ManipulationDetector()
        assessment = detector.assess(posts)

        # All signals low
        assert assessment.duplicate_ratio < 0.3
        assert assessment.source_concentration < 0.4
        assert assessment.coordination_score < 0.3
