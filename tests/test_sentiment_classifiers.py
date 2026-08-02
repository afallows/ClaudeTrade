"""Tests for ``sentiment.classifiers``: options-chatter detection and the
Reddit-flair scoring prior.

``RuleSentimentClassifier`` had no dedicated test module before this file --
these tests exercise the real classifier over hand-built posts, not the
underlying lexicon dicts directly.
"""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.domain import SentimentScores, SocialPost, SocialSource
from claudetrade.sentiment.classifiers import (
    EnsembleSentimentClassifier,
    RuleSentimentClassifier,
    _blend,
)

NOW = dt.datetime(2024, 6, 3, 15, 0, tzinfo=dt.UTC)


def _post(text: str, *, flair: str | None = None) -> SocialPost:
    return SocialPost(
        source=SocialSource.REDDIT,
        external_id="t3_test",
        created_at=NOW,
        text=text,
        flair=flair,
    )


@pytest.fixture
def classifier() -> RuleSentimentClassifier:
    return RuleSentimentClassifier()


class TestOptionsChatterDetection:
    """Calls vs. puts split -- ``SentimentScores.options_call``/``options_put``."""

    def test_bought_calls_is_call_side(self, classifier):
        scores = classifier.classify(_post("bought calls on this today"), "ACME", [])
        assert scores.options_call > 0.0
        assert scores.options_put == 0.0

    def test_call_me_maybe_is_not_call_side(self, classifier):
        """Bare singular 'call' (a phone call, not an options contract) must
        not trip the call-side lexicon -- only plural 'calls' and the
        multi-word phrases do."""
        scores = classifier.classify(_post("call me maybe about this stock"), "ACME", [])
        assert scores.options_call == 0.0
        assert scores.options_put == 0.0

    def test_bought_puts_is_put_side(self, classifier):
        scores = classifier.classify(_post("picked up some puts as a hedge"), "ACME", [])
        assert scores.options_put > 0.0
        assert scores.options_call == 0.0

    def test_put_together_is_not_put_side(self, classifier):
        """Bare singular 'put' in ordinary usage must not trip the put-side
        lexicon."""
        scores = classifier.classify(
            _post("we put together a great presentation for the board"), "ACME", []
        )
        assert scores.options_put == 0.0
        assert scores.options_call == 0.0

    def test_call_options_phrase_detected(self, classifier):
        scores = classifier.classify(_post("loading up on call options here"), "ACME", [])
        assert scores.options_call > 0.0

    def test_put_options_phrase_detected(self, classifier):
        scores = classifier.classify(_post("bought some put options for protection"), "ACME", [])
        assert scores.options_put > 0.0

    def test_strike_shorthand_call_detected(self, classifier):
        """'100C' (no space) reads as a call strike."""
        scores = classifier.classify(_post("grabbed some 100C before earnings"), "ACME", [])
        assert scores.options_call > 0.0
        assert scores.options_put == 0.0

    def test_strike_shorthand_put_detected(self, classifier):
        scores = classifier.classify(_post("grabbed some 100P as insurance"), "ACME", [])
        assert scores.options_put > 0.0
        assert scores.options_call == 0.0

    def test_celsius_is_not_a_call_strike(self, classifier):
        """'100 Celsius' (spelled out, with a space) must not be read as a
        '100C' strike -- the strike shorthand requires no space between the
        number and the letter."""
        scores = classifier.classify(
            _post("it hit 100 Celsius today, wild weather"), "ACME", []
        )
        assert scores.options_call == 0.0

    def test_neutral_text_has_no_options_signal(self, classifier):
        scores = classifier.classify(_post("just watching this one for now"), "ACME", [])
        assert scores.options_call == 0.0
        assert scores.options_put == 0.0

    def test_both_sides_can_fire_together(self, classifier):
        """A spread/hedge mentioning both is not forced into one side --
        mirrors how bullish/bearish can both be non-zero."""
        scores = classifier.classify(
            _post("ran a collar: bought calls and bought puts"), "ACME", []
        )
        assert scores.options_call > 0.0
        assert scores.options_put > 0.0


class TestOptionsSignalExcludedFromAiBlend:
    """The AI schema has no options fields, so ``ai_scores.options_call``/
    ``options_put`` are always the dataclass default (0.0) -- blending them
    in would dilute the rule-derived signal toward zero for no real reason,
    the same problem ``coordinated`` already has and is excluded for."""

    def test_blend_preserves_rule_options_values_unblended(self):
        rule_scores = SentimentScores(
            options_call=0.8, options_put=0.1, confidence=0.5, classifier="rules"
        )
        ai_scores = SentimentScores(confidence=0.9, classifier="ai:test-model")

        blended = _blend(rule_scores, ai_scores)

        assert blended.options_call == pytest.approx(0.8)
        assert blended.options_put == pytest.approx(0.1)


class TestFlairPrior:
    """Reddit's native ``link_flair_text`` gives a small, capped nudge --
    DD/Due-Diligence/Analysis toward catalyst fields, YOLO/Meme/Gain/Loss/
    Shitpost toward hype/pump-and-dump. Neutral (including ``None``) leaves
    scores identical to the no-flair baseline."""

    NEUTRAL_TEXT = "thinking about this company lately"

    def _baseline(self, classifier) -> SentimentScores:
        return classifier.classify(_post(self.NEUTRAL_TEXT, flair=None), "ACME", [])

    @pytest.mark.parametrize("flair", ["DD", "dd", "  DD  ", "Due Diligence", "Analysis"])
    def test_catalyst_flair_boosts_catalyst_fields(self, classifier, flair):
        baseline = self._baseline(classifier)
        scores = classifier.classify(_post(self.NEUTRAL_TEXT, flair=flair), "ACME", [])

        assert scores.earnings_speculation > baseline.earnings_speculation
        assert scores.product_catalyst > baseline.product_catalyst
        assert scores.regulatory_catalyst > baseline.regulatory_catalyst
        # Small, not dominant: nowhere near the top of the [0, 1] range for
        # otherwise-neutral text.
        assert scores.product_catalyst < 0.35

    @pytest.mark.parametrize("flair", ["DD", "Due Diligence", "Analysis"])
    def test_catalyst_flair_does_not_move_hype_or_pump(self, classifier, flair):
        baseline = self._baseline(classifier)
        scores = classifier.classify(_post(self.NEUTRAL_TEXT, flair=flair), "ACME", [])

        assert scores.hype == pytest.approx(baseline.hype)
        assert scores.pump_and_dump == pytest.approx(baseline.pump_and_dump)

    @pytest.mark.parametrize("flair", ["YOLO", "yolo", " Meme ", "Gain", "Loss", "Shitpost"])
    def test_hype_flair_boosts_hype_and_pump(self, classifier, flair):
        baseline = self._baseline(classifier)
        scores = classifier.classify(_post(self.NEUTRAL_TEXT, flair=flair), "ACME", [])

        assert scores.hype > baseline.hype
        assert scores.pump_and_dump > baseline.pump_and_dump
        assert scores.hype < 0.35
        assert scores.pump_and_dump < 0.35

    @pytest.mark.parametrize("flair", ["YOLO", "Meme"])
    def test_hype_flair_does_not_move_catalyst_fields(self, classifier, flair):
        baseline = self._baseline(classifier)
        scores = classifier.classify(_post(self.NEUTRAL_TEXT, flair=flair), "ACME", [])

        assert scores.earnings_speculation == pytest.approx(baseline.earnings_speculation)
        assert scores.product_catalyst == pytest.approx(baseline.product_catalyst)
        assert scores.regulatory_catalyst == pytest.approx(baseline.regulatory_catalyst)

    def test_none_flair_is_unchanged_from_baseline(self, classifier):
        """Explicit unchanged-behaviour check: classifying the same text
        twice with ``flair=None`` gives identical scores -- adding the
        field/prior must not perturb the no-flair path at all."""
        first = classifier.classify(_post(self.NEUTRAL_TEXT, flair=None), "ACME", [])
        second = classifier.classify(_post(self.NEUTRAL_TEXT, flair=None), "ACME", [])
        assert first == second

    @pytest.mark.parametrize("flair", ["Discussion", "News", "Technical Analysis", ""])
    def test_unrecognised_flair_is_neutral(self, classifier, flair):
        """Flair outside both curated sets (including "Technical Analysis",
        which is deliberately NOT in ``FLAIR_CATALYST_TERMS`` -- only the
        exact "Analysis" flair is) behaves identically to no flair at all."""
        baseline = self._baseline(classifier)
        scores = classifier.classify(_post(self.NEUTRAL_TEXT, flair=flair), "ACME", [])
        assert scores == baseline

    def test_flair_boost_is_smaller_than_a_real_lexicon_hit(self, classifier):
        """The flair nudge must not swamp genuine textual evidence -- a post
        that actually contains catalyst language should score higher than a
        DD-flaired post with no such language."""
        real_catalyst_text = "FDA approval expected this quarter, huge product launch coming"
        flaired_neutral = classifier.classify(
            _post(self.NEUTRAL_TEXT, flair="DD"), "ACME", []
        )
        real_signal = classifier.classify(_post(real_catalyst_text), "ACME", [])

        assert real_signal.regulatory_catalyst > flaired_neutral.regulatory_catalyst


def _tagged(text: str, prior: str | None) -> SocialPost:
    """A Stocktwits post carrying (or not carrying) the author's own tag."""
    return SocialPost(
        source=SocialSource.STOCKTWITS,
        external_id=f"st_{abs(hash((text, prior))) % 10**8}",
        created_at=NOW,
        text=text,
        sentiment_prior=prior,
    )


class TestSelfDeclaredSentimentPrior:
    """``_apply_sentiment_prior`` -- the author's own bull/bear tag.

    The tag is the only directional evidence on roughly half of real
    Stocktwits traffic, because the messages that carry it ("$X long",
    "$X 260c", "$X added more") are exactly the ones a lexicon scores flat
    at 0.0/0.0. It is also, being one click, the weakest kind of evidence a
    human can leave, so these tests pin down both that it *counts* and that
    it cannot overrule what someone actually wrote.
    """

    SILENT = "added more here"
    BULL_TEXT = "absolutely mooning, breakout confirmed"
    BEAR_TEXT = "dumping hard, this is a disaster"

    def test_untagged_posts_are_scored_exactly_as_before(self, classifier):
        """The no-tag path must be byte-identical: every Reddit, X and news
        post has ``sentiment_prior is None``, and so does every Stocktwits
        post already on disk (migration 008 backfills nothing)."""
        ensemble = EnsembleSentimentClassifier()
        for text in (self.SILENT, self.BULL_TEXT, self.BEAR_TEXT):
            assert ensemble.classify(_tagged(text, None), "ACME", []) == classifier.classify(
                _tagged(text, None), "ACME", []
            )

    def test_tag_supplies_direction_when_the_text_is_silent(self, classifier):
        """The case that matters: a lexicon-invisible post the author tagged."""
        baseline = classifier.classify(_tagged(self.SILENT, None), "ACME", [])
        assert baseline.bullish == pytest.approx(0.0)
        assert baseline.bearish == pytest.approx(0.0)

        ensemble = EnsembleSentimentClassifier()
        bull = ensemble.classify(_tagged(self.SILENT, "bullish"), "ACME", [])
        bear = ensemble.classify(_tagged(self.SILENT, "bearish"), "ACME", [])

        assert bull.bullish > 0.0 and bull.bearish == pytest.approx(0.0)
        assert bear.bearish > 0.0 and bear.bullish == pytest.approx(0.0)
        assert bull.polarity > 0.0 > bear.polarity

    def test_neutral_is_recomputed_not_left_stale(self):
        """``neutral`` is a residual of bullish+bearish. A post the tag just
        made directional must not still claim to be fully neutral."""
        ensemble = EnsembleSentimentClassifier()
        scores = ensemble.classify(_tagged(self.SILENT, "bullish"), "ACME", [])
        assert scores.neutral == pytest.approx(1.0 - scores.bullish - scores.bearish)
        assert scores.neutral < 1.0

    def test_tag_cannot_overrule_the_text(self):
        """Sarcasm defence: "Bullish" tagged onto plainly bearish writing must
        not flip the read, or every bagholder joke becomes a buy signal."""
        ensemble = EnsembleSentimentClassifier()
        scores = ensemble.classify(_tagged(self.BEAR_TEXT, "bullish"), "ACME", [])
        assert scores.polarity < 0.0
        assert scores.bearish > scores.bullish

    def test_conflict_costs_confidence_and_raises_uncertainty(self, classifier):
        """Tag and text disagreeing means we know *less*, not more."""
        agreeing = classifier.classify(_tagged(self.BEAR_TEXT, None), "ACME", [])
        ensemble = EnsembleSentimentClassifier()
        conflicted = ensemble.classify(_tagged(self.BEAR_TEXT, "bullish"), "ACME", [])

        assert conflicted.confidence < agreeing.confidence
        assert conflicted.uncertainty > agreeing.uncertainty

    def test_agreement_corroborates_without_doubling(self, classifier):
        """Tag and text are one author's single act, not two witnesses."""
        text_only = classifier.classify(_tagged(self.BULL_TEXT, None), "ACME", [])
        ensemble = EnsembleSentimentClassifier()
        corroborated = ensemble.classify(_tagged(self.BULL_TEXT, "bullish"), "ACME", [])

        assert corroborated.bullish > text_only.bullish
        assert corroborated.bullish < text_only.bullish * 2
        assert corroborated.confidence > text_only.confidence

    def test_a_tag_is_worth_less_than_someone_actually_writing_it(self, classifier):
        """One click must never outweigh a typed directional sentence."""
        tagged_silent = EnsembleSentimentClassifier().classify(
            _tagged(self.SILENT, "bullish"), "ACME", []
        )
        written = classifier.classify(_tagged(self.BULL_TEXT, None), "ACME", [])
        assert tagged_silent.bullish < written.bullish

    def test_tag_only_confidence_stays_under_the_downstream_bar(self):
        """A contentless post the author tagged is evidence, but it must not
        clear ``FiltersConfig.min_sentiment_confidence`` (0.35) on its own."""
        scores = EnsembleSentimentClassifier().classify(
            _tagged(self.SILENT, "bullish"), "ACME", []
        )
        assert 0.1 < scores.confidence < 0.35

    @pytest.mark.parametrize("prior", [None, "", "neutral", "Bullish", "unknown"])
    def test_only_normalised_tags_are_honoured(self, classifier, prior):
        """The provider normalises to "bullish"/"bearish"/None; anything else
        reaching here is a bug upstream and must be ignored, not guessed at.
        Note "Bullish" -- the raw Stocktwits casing -- is deliberately *not*
        accepted, so a provider that forgets to normalise fails loudly (as a
        missing effect) rather than half-working."""
        expected = classifier.classify(_tagged(self.SILENT, None), "ACME", [])
        actual = EnsembleSentimentClassifier().classify(_tagged(self.SILENT, prior), "ACME", [])
        assert (actual.bullish, actual.bearish) == (expected.bullish, expected.bearish)

    def test_provenance_records_that_a_tag_was_applied(self):
        """``classifier`` is the provenance breadcrumb for "why is this row
        directional when the text reads flat?"."""
        applied = EnsembleSentimentClassifier().classify(
            _tagged(self.SILENT, "bullish"), "ACME", []
        )
        untouched = EnsembleSentimentClassifier().classify(_tagged(self.SILENT, None), "ACME", [])
        assert applied.classifier.endswith("+prior")
        assert not untouched.classifier.endswith("+prior")
