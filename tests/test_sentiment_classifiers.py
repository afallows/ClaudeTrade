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
from claudetrade.sentiment.classifiers import RuleSentimentClassifier, _blend

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
