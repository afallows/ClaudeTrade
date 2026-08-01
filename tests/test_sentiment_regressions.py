"""Regression tests for the 2026-07-31 zero-signal incident (QA handoff v2).

Each test pins the fix for a specific finding:

* F19 -- the rule classifier returned bullish=bearish=0.0 for virtually all
  real social text because lexicon phrases never matched across punctuation.
* F18 -- aggregate sentiment confidence *decreased* for newer sessions and
  was calibrated so low (0.02-0.08) that every confidence gate was
  structurally unreachable.
* F16/F15 -- ``build_sentiment`` wrote a current-session row byte-identical
  to yesterday's whenever nothing new had been fetched, and refreshes
  rewrote historical rows from whatever thin post set they re-fetched.
* F14 (residual) -- extraction paths that still let ordinary English mint
  actionable ticker mentions, and alias mentions whose stored context was
  useless for classification.
* Strategy-layer bugs surfaced while diagnosing the incident: the AMC
  event-bar off-by-one in post-earnings drift, and percentile saturation on
  constant histories.
"""

from __future__ import annotations

import datetime as dt

from claudetrade.config import SentimentConfig
from claudetrade.domain import (
    EarningsEvent,
    EarningsSession,
    SecurityInfo,
    SocialPost,
    SocialSource,
    TickerMention,
)
from claudetrade.sentiment.aggregation import SentimentAggregator
from claudetrade.sentiment.classifiers import RuleSentimentClassifier
from claudetrade.sentiment.entity_resolution import TickerResolver
from claudetrade.strategies.scoring_utils import percentile_rank

_SESSION = dt.date(2026, 7, 31)
_CLOSE = dt.datetime(2026, 7, 31, 20, 0, tzinfo=dt.UTC)


def _post(text: str, *, external_id: str = "p1", author: str = "author1",
          age_hours: float = 3.0, score: int = 10) -> SocialPost:
    return SocialPost(
        source=SocialSource.REDDIT,
        external_id=external_id,
        created_at=_CLOSE - dt.timedelta(hours=age_hours),
        text=text,
        author_hash=author,
        score=score,
    )


def _classify(text: str):
    return RuleSentimentClassifier().classify(_post(text), "TEST", [])


class TestPunctuationMatching:
    """F19: lexicon terms must match across clause-boundary punctuation."""

    def test_trailing_comma_does_not_break_a_match(self):
        scores = _classify("AMZN crushed earnings, the stock is ripping today")
        assert scores.bullish > 0.3
        assert scores.polarity > 0.3

    def test_trailing_period_and_exclamation_do_not_break_a_match(self):
        assert _classify("This is bearish.").bearish > 0.3
        assert _classify("Very bullish!").bullish > 0.3

    def test_newline_separated_sentences_match(self):
        scores = _classify("first line\nthis is bullish\nlast line")
        assert scores.bullish > 0.3

    def test_bearish_text_reads_negative(self):
        scores = _classify("AAPL missed earnings, cut guidance. Selling everything.")
        assert scores.bearish > 0.4
        assert scores.polarity < -0.3

    def test_negation_still_flips_polarity(self):
        scores = _classify("not bullish at all here")
        assert scores.bearish > 0.0
        assert scores.polarity < 0.0


class TestSarcasmMarkerScoping:
    """The '/s' marker must fire as a standalone token, never inside a URL."""

    def test_url_path_does_not_read_as_sarcasm(self):
        scores = _classify("Check the DD at reddit.com/r/stocks - very bullish here")
        assert scores.sarcasm < 0.3
        assert scores.polarity > 0.3

    def test_standalone_marker_still_fires(self):
        scores = _classify("great idea, definitely buy the top /s")
        assert scores.sarcasm > 0.5


def _bullish_corpus(n: int = 10) -> tuple[list[SocialPost], list[TickerMention], dict]:
    clf = RuleSentimentClassifier()
    texts = [
        "AMZN crushed earnings, absolutely ripping today. Buying more calls!",
        "Great earnings, guidance raised. Bullish.",
        "Beat expectations, this is going higher. Loading up.",
        "The quarter was strong, staying long.",
        "To the moon after that blowout quarter",
        "I was bearish but they proved me wrong, crushed it.",
        "Printing money. Growth is back.",
        "Best stock in my portfolio, calls printing.",
        "Solid quarter. Long term hold for me.",
        "Undervalued even after the pop, buying more.",
    ]
    posts, mentions, scores = [], [], {}
    for i in range(n):
        post = _post(texts[i % len(texts)], external_id=f"p{i}", author=f"author{i}",
                     age_hours=2.0 + i)
        posts.append(post)
        mention = TickerMention(
            post_external_id=post.external_id, symbol="AMZN", confidence=0.92,
            method="cashtag", matched_text="$AMZN", context=post.text,
        )
        mentions.append(mention)
        scores[post.external_id] = clf.classify(post, "AMZN", [mention])
    return posts, mentions, scores


class TestConfidenceCalibration:
    """F18/F17: a healthy sample must clear the gates; stale must not."""

    def test_healthy_fresh_sample_clears_the_sentiment_gate(self):
        posts, mentions, scores = _bullish_corpus()
        snap = SentimentAggregator(SentimentConfig()).aggregate(
            "AMZN", _SESSION, posts, mentions, scores
        )
        # FilterConfig.min_sentiment_confidence is 0.35; the broken
        # calibration produced 0.02-0.08 for samples exactly like this one.
        assert snap.confidence >= 0.35
        assert snap.raw_sentiment > 0.2
        assert snap.bull_bear_ratio > 1.5

    def test_confidence_falls_when_the_same_posts_merely_age(self):
        posts, mentions, scores = _bullish_corpus()
        agg = SentimentAggregator(SentimentConfig())
        fresh = agg.aggregate("AMZN", _SESSION, posts, mentions, scores)
        # Two sessions later with nothing new fetched: the identical post set
        # must be trusted LESS, not the same (and never more).
        stale = agg.aggregate("AMZN", _SESSION + dt.timedelta(days=3), posts, mentions, scores)
        assert stale.confidence < fresh.confidence

    def test_old_posts_behind_fresh_ones_cost_little(self):
        """Decay-weighted staleness: a week of old posts plus a fresh burst
        must read close to the fresh burst alone, not be dragged to stale."""
        posts, mentions, scores = _bullish_corpus()
        clf = RuleSentimentClassifier()
        old_texts = [
            "bullish on the roadmap after the keynote",
            "added more last week, undervalued imo",
            "long term hold, ignoring the noise",
            "great earnings last quarter, still holding",
            "accumulating slowly on red days",
            "the chart looks like a breakout forming",
            "buy the dip worked again for me",
            "price target raised by two analysts",
            "loading up before the conference",
            "new highs coming, mark my words",
        ]
        old_posts = []
        for i in range(10):
            post = _post(old_texts[i], external_id=f"old{i}",
                         author=f"oldauthor{i}", age_hours=24.0 * 6 + i)
            old_posts.append(post)
            mention = TickerMention(
                post_external_id=post.external_id, symbol="AMZN", confidence=0.92,
                method="cashtag", matched_text="$AMZN", context=post.text,
            )
            mentions.append(mention)
            scores[post.external_id] = clf.classify(post, "AMZN", [mention])
        agg = SentimentAggregator(SentimentConfig())
        fresh_only = agg.aggregate("AMZN", _SESSION, posts, mentions[:10], scores)
        mixed = agg.aggregate("AMZN", _SESSION, posts + old_posts, mentions, scores)
        assert mixed.confidence > fresh_only.confidence * 0.6

    def test_catalyst_labels_are_surfaced_individually(self):
        """The capitulation strategy's regulatory-catalyst veto reads
        labels['regulatory_catalyst']; the aggregator must populate it."""
        posts, mentions, scores = _bullish_corpus(3)
        snap = SentimentAggregator(SentimentConfig()).aggregate(
            "AMZN", _SESSION, posts, mentions, scores
        )
        assert "regulatory_catalyst" in snap.labels
        assert "earnings_speculation" in snap.labels
        assert "product_catalyst" in snap.labels


class TestBuildSentimentSessionWindows:
    """F16/F15: rows are written only for sessions that gained posts."""

    def test_no_carry_forward_row_for_a_session_with_no_new_posts(
        self, tmp_app_config, tmp_db
    ):
        from claudetrade.pipeline import Pipeline

        pipeline = Pipeline(tmp_app_config, tmp_db)
        directory = {"AMZN": SecurityInfo(symbol="AMZN", name="Amazon.com Inc")}
        posts, _, _ = _bullish_corpus()
        # Posts all land within a few hours before the 2026-07-31 close.
        written = pipeline.build_sentiment(
            posts=posts, directory=directory,
            start=_SESSION - dt.timedelta(days=2),
            end=_SESSION + dt.timedelta(days=4),
        )
        from sqlalchemy import select

        from claudetrade.db.models import SymbolSentimentDaily

        with tmp_db.read_session() as session:
            sessions = sorted(
                session.execute(select(SymbolSentimentDaily.session)).scalars().all()
            )
        # Exactly one session gained posts, so exactly one row exists -- no
        # fabricated rows for 08-03/08-04 carrying 07-31's numbers forward.
        assert written == 1
        assert sessions == [_SESSION]

    def test_refetch_without_in_window_posts_leaves_history_alone(
        self, tmp_app_config, tmp_db
    ):
        from sqlalchemy import select

        from claudetrade.db.models import SymbolSentimentDaily
        from claudetrade.pipeline import Pipeline

        pipeline = Pipeline(tmp_app_config, tmp_db)
        directory = {"AMZN": SecurityInfo(symbol="AMZN", name="Amazon.com Inc")}
        posts, _, _ = _bullish_corpus()
        pipeline.build_sentiment(
            posts=posts, directory=directory,
            start=_SESSION, end=_SESSION,
        )
        with tmp_db.read_session() as session:
            original = session.execute(select(SymbolSentimentDaily)).scalars().one()
            original_count = original.post_count

        # A later "refresh" whose fetch only returned two of the posts must
        # not degrade the stored row for that session: none of its posts are
        # new for the session window... but they ARE in-window, so the row IS
        # recomputed -- from the same-session posts only. The protection this
        # asserts is for sessions with NO in-window posts at all.
        later_posts = [
            _post("totally unrelated day, bullish on something else",
                  external_id="later1", author="later1",
                  age_hours=-24.0 * 3)  # three days AFTER the close
        ]
        pipeline.build_sentiment(
            posts=later_posts, directory=directory,
            start=_SESSION, end=_SESSION,
        )
        with tmp_db.read_session() as session:
            after = session.execute(select(SymbolSentimentDaily)).scalars().all()
            by_session = {r.session: r for r in after}
        assert by_session[_SESSION].post_count == original_count


class TestExtractionResiduals:
    """F14 residuals: paths that still minted actionable junk mentions."""

    def _resolver(self) -> TickerResolver:
        directory = {
            "CASH": SecurityInfo(symbol="CASH", name="Pathward Financial"),
            "BBY": SecurityInfo(symbol="BBY", name="Best Buy Co"),
            "CSCO": SecurityInfo(symbol="CSCO", name="Cisco Systems"),
            "NVDA": SecurityInfo(symbol="NVDA", name="NVIDIA Corporation"),
        }
        return TickerResolver(directory=directory)

    def test_lowercase_cashtag_of_a_common_word_is_not_actionable(self):
        post = _post("paying in $cash is the only real way")
        mentions = {m.symbol: m for m in self._resolver().resolve(post)}
        assert "CASH" not in mentions or mentions["CASH"].confidence < 0.60

    def test_uppercase_cashtag_keeps_full_credit(self):
        post = _post("loading $CASH before the report")
        mentions = {m.symbol: m for m in self._resolver().resolve(post)}
        assert mentions["CASH"].confidence >= 0.90

    def test_multi_word_company_names_keep_the_flat_name_base(self):
        """Brand words are IN the common-words corpus (companies make their
        own names common), so a per-token ambiguity test would demote Bank
        of America / Morgan Stanley / Home Depot-class names. Multi-word
        names must keep the flat base even when casual text uses them."""
        post = _post("Best Buy is breaking out this morning")
        mentions = {m.symbol: m for m in self._resolver().resolve(post)}
        assert mentions["BBY"].confidence >= 0.60

    def test_distinctive_company_name_still_resolves(self):
        post = _post("Cisco Systems just crushed earnings, guidance raised, buying calls")
        mentions = {m.symbol: m for m in self._resolver().resolve(post)}
        assert mentions["CSCO"].confidence >= 0.60

    def test_name_mention_context_carries_the_sentiment_bearing_clause(self):
        """The stored context must be a window of the post, not the bare
        alias -- the classifier scores context in preference to full text,
        and 'cisco systems' alone classifies neutral."""
        text = "Cisco Systems just crushed earnings, guidance raised, buying more calls"
        post = _post(text)
        mentions = {m.symbol: m for m in self._resolver().resolve(post)}
        ctx = mentions["CSCO"].context
        assert "crushed earnings" in ctx
        scores = RuleSentimentClassifier().classify(post, "CSCO", [mentions["CSCO"]])
        assert scores.bullish > 0.3


class TestPercentileRankDegeneracy:
    def test_constant_history_is_neutral_not_top_percentile(self):
        assert percentile_rank([0.0, 0.0, 0.0, 0.0], 0.0) == 0.5

    def test_informative_history_still_ranks(self):
        assert percentile_rank([1.0, 2.0, 3.0], 3.0) == 1.0
        assert percentile_rank([1.0, 2.0, 3.0, 4.0], 1.0) == 0.25


class TestPostEarningsEventBar:
    """The AMC off-by-one: an after-close report's reaction is the NEXT bar."""

    def _event(self, session: EarningsSession) -> EarningsEvent:
        return EarningsEvent(
            symbol="AMZN",
            report_date=dt.date(2026, 7, 30),
            session=session,
            confirmed=True,
            surprise_pct=12.0,
        )

    def test_amc_report_uses_the_next_session_bar(self):
        from claudetrade.strategies.e_post_earnings_drift import PostEarningsDriftStrategy

        amc = PostEarningsDriftStrategy._first_reaction_session(self._event(EarningsSession.AFTER_CLOSE))
        bmo = PostEarningsDriftStrategy._first_reaction_session(self._event(EarningsSession.BEFORE_OPEN))
        assert amc == dt.date(2026, 7, 31)
        assert bmo == dt.date(2026, 7, 30)
