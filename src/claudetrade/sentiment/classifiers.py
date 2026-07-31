"""Deterministic and ensemble sentiment classifiers.

``RuleSentimentClassifier`` is the load-bearing component: it must produce a
usable ``SentimentScores`` for every post with **no** network access and no AI
credential configured, because that is the default operating mode of this
application (``AIConfig.provider == "null"``). ``EnsembleSentimentClassifier``
layers an optional AI opinion on top, weighted by relative confidence, and
degrades to rules-only whenever the AI classifier has nothing to contribute.

**Honest limitation**: the rule classifier is a lexicon-and-heuristics scanner
(see ``sentiment.lexicon`` for the documented limits of that approach). It
will misread genuinely novel phrasing, complex multi-clause negation, and
sarcasm that carries no marker word. Its ``confidence`` output is the pipeline's
best attempt to say "trust this less" in exactly those situations, not a
guarantee of correctness.
"""

from __future__ import annotations

import logging
import re

from claudetrade.domain import SentimentScores, SocialPost, TickerMention
from claudetrade.sentiment.ai_classifier import AISentimentClassifier
from claudetrade.sentiment.lexicon import (
    BEARISH_EMOJI,
    BEARISH_TERMS,
    BULLISH_EMOJI,
    BULLISH_TERMS,
    CAPITULATION_TERMS,
    DIMINISHERS,
    EARNINGS_SPECULATION_TERMS,
    FEAR_TERMS,
    FLAIR_CATALYST_TERMS,
    FLAIR_HYPE_TERMS,
    HYPE_FOMO_TERMS,
    INTENSIFIERS,
    NEGATION_WINDOW,
    NEGATORS,
    OPTIONS_CALL_TERMS,
    OPTIONS_PUT_TERMS,
    POSITION_DISCLOSURE_PATTERNS,
    PRODUCT_CATALYST_TERMS,
    PUMP_DUMP_TEMPLATES,
    REGULATORY_CATALYST_TERMS,
    RUMOUR_MARKERS,
    SARCASM_MARKERS,
    SHORT_SQUEEZE_TERMS,
    UNCERTAINTY_HEDGE_TERMS,
)

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9']+")
_EXCESS_PUNCT_RE = re.compile(r"[!?]{2,}")
_ALLCAPS_TOKEN_RE = re.compile(r"\b[A-Z]{2,}\b")
#: Strike-shorthand ("100c" / "100p"): digits immediately (no space) followed
#: by the bare letter, word-bounded so "100 Celsius" or "scored 100 points"
#: don't match -- only a directly-appended letter reads as options slang.
#: Run against the already-lower-cased ``text_norm``, so no IGNORECASE flag
#: is needed here. Idea (the `\d+C`/`\d+P` shape): Stocksera
#: (``scheduled_tasks/reddit/stocks/scrape_discussion_thread.py``, MIT).
_OPTIONS_CALL_STRIKE_RE = re.compile(r"\b\d+c\b")
_OPTIONS_PUT_STRIKE_RE = re.compile(r"\b\d+p\b")
#: Modest, capped nudges from Reddit's own flair -- see
#: ``lexicon.FLAIR_CATALYST_TERMS``/``FLAIR_HYPE_TERMS`` for the term sets
#: and provenance. Sized in the same range as the other ad-hoc adjustments
#: in ``classify`` below (excess-punctuation, emoji, ...), well under a
#: single average-weight lexicon phrase hit -- a self-applied label is
#: weaker evidence than the classifier's own read of the text.
_FLAIR_CATALYST_BOOST = 0.25
_FLAIR_HYPE_BOOST = 0.25
_FLAIR_PUMP_BOOST = 0.2


def _normalise(text: str) -> str:
    """Lower-case and strip apostrophes so lexicon phrases match consistently.

    Contractions ("don't") and their lexicon keys ("dont") must line up, or
    the negation check below silently never fires.
    """
    return text.lower().replace("'", "").replace("\u2019", "")


def _find_hits(text_norm: str, lexicon: dict[str, float]) -> list[tuple[int, str, float]]:
    """Every ``(start_index, phrase, weight)`` occurrence of a lexicon phrase."""
    hits: list[tuple[int, str, float]] = []
    for phrase, weight in lexicon.items():
        needle = phrase if phrase.startswith("/") else f" {phrase} "
        haystack = text_norm if phrase.startswith("/") else f" {text_norm} "
        start = 0
        while True:
            pos = haystack.find(needle, start)
            if pos == -1:
                break
            hits.append((pos, phrase, weight))
            start = pos + max(1, len(needle))
    return hits


def _preceding_words(text_norm: str, start: int, n: int) -> list[str]:
    prefix = text_norm[:start]
    words = _WORD_RE.findall(prefix)
    return words[-n:] if n > 0 else []


def _is_negated(text_norm: str, start: int) -> bool:
    return any(w in NEGATORS for w in _preceding_words(text_norm, start, NEGATION_WINDOW))


def _modifier_multiplier(text_norm: str, start: int) -> float:
    """Nearest preceding intensifier/diminisher, if any (closest one wins)."""
    words = _preceding_words(text_norm, start, NEGATION_WINDOW)
    for word in reversed(words):
        if word in INTENSIFIERS:
            return INTENSIFIERS[word]
        if word in DIMINISHERS:
            return DIMINISHERS[word]
    return 1.0


def _directional_scores(text_norm: str) -> tuple[float, float]:
    """Bullish/bearish raw magnitudes, with local negation flipping polarity.

    A negated bullish hit mostly cancels its own bullish contribution and
    bleeds a smaller amount into bearish (and symmetrically for a negated
    bearish hit) -- "not bullish at all" should read as leaning bearish/flat,
    not as a full-strength bearish statement invented from nothing.
    """
    bullish = 0.0
    bearish = 0.0
    for start, _phrase, weight in _find_hits(text_norm, BULLISH_TERMS):
        w = weight * _modifier_multiplier(text_norm, start)
        if _is_negated(text_norm, start):
            bullish -= w * 0.5
            bearish += w * 0.5
        else:
            bullish += w
    for start, _phrase, weight in _find_hits(text_norm, BEARISH_TERMS):
        w = weight * _modifier_multiplier(text_norm, start)
        if _is_negated(text_norm, start):
            bearish -= w * 0.5
            bullish += w * 0.5
        else:
            bearish += w
    return max(0.0, bullish), max(0.0, bearish)


def _lexicon_score(text_norm: str, lexicon: dict[str, float]) -> tuple[float, int]:
    """Un-negated-aware magnitude for lexicons where negation matters less
    than raw presence (hype, fear, catalysts, rumours, ...), plus hit count."""
    hits = _find_hits(text_norm, lexicon)
    total = 0.0
    for start, _phrase, weight in hits:
        w = weight * _modifier_multiplier(text_norm, start)
        if _is_negated(text_norm, start):
            w *= 0.3  # damped, not inverted -- "not exactly hype" still reads as low-key hype-adjacent
        total += w
    return total, len(hits)


def _squash(x: float, scale: float = 1.4) -> float:
    """Map a non-negative raw magnitude onto (0, 1) with diminishing returns."""
    if x <= 0:
        return 0.0
    return 1.0 - pow(2.718281828, -x / scale)


class RuleSentimentClassifier:
    """Deterministic, lexicon-and-heuristics sentiment classifier.

    Works with zero external dependencies; this is the mandatory floor every
    post/symbol pair gets, AI-assisted or not.
    """

    def classify(
        self, post: SocialPost, symbol: str, mentions: list[TickerMention]
    ) -> SentimentScores:
        """Score ``post`` for ``symbol``.

        ``mentions`` lets the caller hand in a symbol-specific text window
        (``TickerMention.context``) when a post discusses several tickers with
        different sentiment for each; falling back to the full post text is
        the reasonable default when no narrower context is available.
        """
        context = next(
            (m.context for m in mentions if m.symbol == symbol and m.context), post.text
        )
        text = context or post.text or ""
        text_norm = _normalise(text)
        words = _WORD_RE.findall(text_norm)
        word_count = max(1, len(words))

        bullish_raw, bearish_raw = _directional_scores(text_norm)

        hype_raw, hype_hits = _lexicon_score(text_norm, HYPE_FOMO_TERMS)
        fear_raw, fear_hits = _lexicon_score(text_norm, FEAR_TERMS)
        capitulation_raw, capit_hits = _lexicon_score(text_norm, CAPITULATION_TERMS)
        uncertainty_raw, unc_hits = _lexicon_score(text_norm, UNCERTAINTY_HEDGE_TERMS)
        sarcasm_raw, sarcasm_hits = _lexicon_score(text_norm, SARCASM_MARKERS)
        earnings_raw, _ = _lexicon_score(text_norm, EARNINGS_SPECULATION_TERMS)
        product_raw, _ = _lexicon_score(text_norm, PRODUCT_CATALYST_TERMS)
        regulatory_raw, _ = _lexicon_score(text_norm, REGULATORY_CATALYST_TERMS)
        rumour_raw, _ = _lexicon_score(text_norm, RUMOUR_MARKERS)
        squeeze_raw, _ = _lexicon_score(text_norm, SHORT_SQUEEZE_TERMS)
        pump_raw, pump_hits = _lexicon_score(text_norm, PUMP_DUMP_TEMPLATES)
        position_raw, _ = _lexicon_score(text_norm, POSITION_DISCLOSURE_PATTERNS)
        call_raw, _ = _lexicon_score(text_norm, OPTIONS_CALL_TERMS)
        put_raw, _ = _lexicon_score(text_norm, OPTIONS_PUT_TERMS)

        # Strike shorthand ("100c"/"100p") isn't a literal phrase, so it is
        # scanned separately from the phrase lexicons above (see
        # ``_OPTIONS_CALL_STRIKE_RE`` for why); each hit contributes about
        # as much as one below-average-weight phrase match.
        call_raw += 0.5 * len(_OPTIONS_CALL_STRIKE_RE.findall(text_norm))
        put_raw += 0.5 * len(_OPTIONS_PUT_STRIKE_RE.findall(text_norm))

        # Emoji: cheap, reliable, source-independent bullish/bearish/hype signal.
        bullish_emoji_hits = sum(text.count(e) for e in BULLISH_EMOJI)
        bearish_emoji_hits = sum(text.count(e) for e in BEARISH_EMOJI)
        bullish_raw += 0.5 * bullish_emoji_hits
        bearish_raw += 0.5 * bearish_emoji_hits
        hype_raw += 0.4 * bullish_emoji_hits
        fear_raw += 0.4 * bearish_emoji_hits

        # ALL-CAPS and exclamation density: cheap proxies for hype/excitement
        # that a lexicon of words alone would miss ("THIS IS IT!!!").
        allcaps_hits = len(_ALLCAPS_TOKEN_RE.findall(text))
        allcaps_density = allcaps_hits / word_count
        exclaim_density = text.count("!") / max(1, len(text)) * 40.0
        hype_raw += min(1.0, allcaps_density * 2.0) + min(0.6, exclaim_density)

        # Excessive punctuation ("???", "!!!") is a classic sarcasm/irony tell.
        excess_punct_hits = len(_EXCESS_PUNCT_RE.findall(text))
        sarcasm_raw += 0.3 * excess_punct_hits
        if "/s" in text.lower():
            sarcasm_raw += 0.9

        # Question forms read as uncertainty, independent of lexicon hits.
        question_marks = text.count("?")
        uncertainty_raw += min(1.0, 0.35 * question_marks)

        pump_raw += 0.3 * squeeze_raw  # squeeze hype compounds pump-and-dump language

        # Reddit's own `link_flair_text`, when present, is a cheap,
        # deterministic post-type hint the author/community attached --
        # not proof of anything, so the nudge stays modest (see
        # `_FLAIR_*_BOOST` above) and never manufactures a catalyst or
        # pump-and-dump read out of otherwise-neutral text on its own; it
        # only amplifies whatever the lexicon scan already found. `None`
        # or any flair outside the two curated sets is neutral -- no
        # adjustment at all, identical to today's behaviour.
        flair_norm = (post.flair or "").strip().casefold()
        if flair_norm in FLAIR_CATALYST_TERMS:
            earnings_raw += _FLAIR_CATALYST_BOOST
            product_raw += _FLAIR_CATALYST_BOOST
            regulatory_raw += _FLAIR_CATALYST_BOOST
        elif flair_norm in FLAIR_HYPE_TERMS:
            hype_raw += _FLAIR_HYPE_BOOST
            pump_raw += _FLAIR_PUMP_BOOST

        bullish = _squash(bullish_raw)
        bearish = _squash(bearish_raw)
        hype = _squash(hype_raw)
        fear = _squash(fear_raw)
        capitulation = _squash(capitulation_raw)
        uncertainty = min(1.0, _squash(uncertainty_raw))
        sarcasm = min(1.0, _squash(sarcasm_raw))
        earnings_speculation = _squash(earnings_raw)
        product_catalyst = _squash(product_raw)
        regulatory_catalyst = _squash(regulatory_raw)
        rumour = _squash(rumour_raw)
        short_squeeze = _squash(squeeze_raw)
        pump_and_dump = _squash(pump_raw)
        position_disclosure = _squash(position_raw)
        options_call = _squash(call_raw)
        options_put = _squash(put_raw)
        fomo = _squash(hype_raw * 0.7)  # fomo tracks hype but is not identical to it

        total_signal = bullish + bearish
        neutral = max(0.0, 1.0 - total_signal)

        total_hits = (
            hype_hits + fear_hits + capit_hits + unc_hits + sarcasm_hits + pump_hits
        ) + int(bullish_raw > 0) + int(bearish_raw > 0)

        confidence = _confidence(
            word_count=word_count,
            evidence_hits=total_hits,
            sarcasm=sarcasm,
            bullish=bullish,
            bearish=bearish,
        )

        return SentimentScores(
            bullish=bullish,
            bearish=bearish,
            neutral=neutral,
            uncertainty=uncertainty,
            sarcasm=sarcasm,
            fear=fear,
            hype=hype,
            fomo=fomo,
            capitulation=capitulation,
            earnings_speculation=earnings_speculation,
            product_catalyst=product_catalyst,
            regulatory_catalyst=regulatory_catalyst,
            rumour=rumour,
            short_squeeze=short_squeeze,
            pump_and_dump=pump_and_dump,
            position_disclosure=position_disclosure,
            options_call=options_call,
            options_put=options_put,
            # Coordination is a cross-post property (near-identical text from
            # many authors); a single-post rule classifier has no basis to
            # score it and leaves it to `sentiment.manipulation` /
            # `sentiment.aggregation`, which see the whole post set.
            coordinated=0.0,
            confidence=confidence,
            classifier="rules",
        )


def _confidence(
    *, word_count: int, evidence_hits: int, sarcasm: float, bullish: float, bearish: float
) -> float:
    """Explicit multiplicative confidence combination.

    Each factor independently discounts confidence toward zero; none of them
    can push it back up past 1.0 on its own. Documented per-factor:

    * ``length_factor`` -- very short posts ("bullish", 1 word) are weak
      evidence even when the one word present is unambiguous.
    * ``evidence_factor`` -- more independent lexicon hits corroborate the
      read; a bare emoji or single hedge word alone is thin.
    * ``sarcasm_factor`` -- heavy sarcasm means the surface polarity is
      unreliable (see ``SentimentScores.polarity``), so trust in the labels
      generally, not just polarity, drops.
    * ``conflict_factor`` -- both bullish and bearish firing strongly at once
      signals a genuinely mixed or confused read, not two independent facts.
    """
    length_factor = max(0.2, min(1.0, word_count / 6.0))
    evidence_factor = max(0.25, min(1.0, 0.25 + 0.15 * evidence_hits))
    sarcasm_factor = max(0.25, 1.0 - 0.65 * sarcasm)
    conflict = min(bullish, bearish)
    conflict_factor = max(0.3, 1.0 - min(0.7, conflict * 1.6))

    confidence = evidence_factor * length_factor * sarcasm_factor * conflict_factor
    return max(0.05, min(0.97, confidence))


class EnsembleSentimentClassifier:
    """Combines the rule classifier with an optional AI classifier.

    The rule classifier always runs -- it is the floor. When an AI classifier
    is configured *and* returns a usable result, the two are blended weighted
    by each source's own ``confidence``; otherwise this degrades silently
    (and loudly in the logs) to rules-only.
    """

    def __init__(
        self,
        rule_classifier: RuleSentimentClassifier | None = None,
        ai_classifier: AISentimentClassifier | None = None,
    ):
        self.rule_classifier = rule_classifier or RuleSentimentClassifier()
        self.ai_classifier = ai_classifier

    def classify(
        self, post: SocialPost, symbol: str, mentions: list[TickerMention]
    ) -> SentimentScores:
        rule_scores = self.rule_classifier.classify(post, symbol, mentions)

        if self.ai_classifier is None:
            return rule_scores

        # `AISentimentClassifier.classify` returns None for every failure mode
        # (no credential, malformed JSON, schema violation, cost cap, blocked
        # by injection risk) -- by contract it never raises and never returns
        # a `parsed_ok=False` result for us to accidentally trust.
        ai_scores = self.ai_classifier.classify(post, symbol)
        if ai_scores is None:
            log.debug("ai classifier unavailable for %s/%s; using rules only", symbol, post.external_id)
            return rule_scores

        return _blend(rule_scores, ai_scores)


_BLENDABLE_FIELDS = (
    "bullish",
    "bearish",
    "neutral",
    "uncertainty",
    "sarcasm",
    "fear",
    "hype",
    "fomo",
    "capitulation",
    "earnings_speculation",
    "product_catalyst",
    "regulatory_catalyst",
    "rumour",
    "short_squeeze",
    "pump_and_dump",
    "position_disclosure",
)


def _blend(rule_scores: SentimentScores, ai_scores: SentimentScores) -> SentimentScores:
    """Confidence-weighted average of every label field.

    ``coordinated`` is deliberately excluded from the AI side of the blend --
    it is a cross-post property the AI classifier (which only ever sees one
    post at a time) cannot legitimately assess; the rule value (always 0.0 at
    this stage, populated later by aggregation) passes through unchanged.

    ``options_call``/``options_put`` are excluded for the same reason as
    ``coordinated``, just for a different cause: the AI schema
    (``ai_classifier._REQUIRED_FIELDS``) has no options-chatter fields, so
    ``ai_scores`` always carries the dataclass default (0.0) there -- if
    these were blended, every AI-assisted post would have its rule-derived
    options signal diluted toward zero in proportion to the AI's confidence,
    which is not a real "the AI disagrees" signal, just an artefact of the
    field not being asked for. The rule value passes through unchanged.
    """
    w_rule = max(1e-6, rule_scores.confidence)
    w_ai = max(0.0, ai_scores.confidence)
    total = w_rule + w_ai
    blended: dict[str, float] = {}
    for field_name in _BLENDABLE_FIELDS:
        rv = getattr(rule_scores, field_name)
        av = getattr(ai_scores, field_name)
        blended[field_name] = (rv * w_rule + av * w_ai) / total

    # Two independently-derived readings agreeing is itself evidence; simple
    # confidence-weighted averaging (rather than a max) reflects that a
    # confident rule read plus a confident AI read should not score *lower*
    # than either alone.
    combined_confidence = max(0.0, min(1.0, (w_rule * w_rule + w_ai * w_ai) / total))

    return SentimentScores(
        **blended,
        coordinated=rule_scores.coordinated,
        options_call=rule_scores.options_call,
        options_put=rule_scores.options_put,
        confidence=combined_confidence,
        classifier=f"ensemble(rules+{ai_scores.classifier})",
    )
