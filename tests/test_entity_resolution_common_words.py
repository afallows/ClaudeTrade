"""QA F25 adversarial regression: the ten stopword tickers that owned trending.

QA handoff v3 (2026-08-01) found the trending list still dominated by
AS/YOU/AN/DAY/NEXT/AM/REAL/CASH/TWO/WAY -- every one a REAL NYSE/Nasdaq
listing (Amer Sports, Clear Secure, AutoNation, Dayforce, NextDecade, Antero
Midstream, The RealReal, Pathward, Two Harbors, Waystar) colliding with an
extremely common English word, so ``get_trending``'s securities join cannot
filter them. The junk QA saw was stale stored aggregates from pre-hardening
extraction (healed by ``sentiment.rebuild``); this module pins that the
CURRENT extractor cannot mint such mentions again, at the pipeline's real
operating threshold (``SentimentConfig.min_ticker_confidence``), against a
directory that really contains all ten securities.

The positive half pins "discounted, never blocked": cashtags and company
names for these symbols still clear the threshold, and a bare mention EARNS
confidence from finance context -- while deliberately staying below the
actionable pipeline threshold, because social-finance prose is finance
context *by construction*: any rule that let nearby trading vocabulary carry
a bare common word over the bar would re-mint AS out of every "BOUGHT CALLS
AS THE MARKET RIPPED" shouting post, which is precisely the F25 failure.
"""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.config import SentimentConfig
from claudetrade.domain import SecurityInfo, SocialPost, SocialSource
from claudetrade.sentiment.classifiers import RuleSentimentClassifier
from claudetrade.sentiment.common_words import COMMON_WORDS_AND_ACRONYMS
from claudetrade.sentiment.entity_resolution import TickerResolver, is_ambiguous_symbol

#: The pipeline's actual mention-counting floor: ``Pipeline.build_sentiment``
#: and ``DataIngestor.resolve_and_persist_mentions`` both drop mentions below
#: ``config.sentiment.min_ticker_confidence``. The tests below run at this
#: default deliberately -- a test at a made-up threshold proves nothing about
#: what production stores.
THRESHOLD = SentimentConfig().min_ticker_confidence

#: The ten QA v3 offenders: (symbol, listed company name).
QA_TEN: list[tuple[str, str]] = [
    ("AS", "Amer Sports, Inc."),
    ("YOU", "Clear Secure, Inc."),
    ("AN", "AutoNation, Inc."),
    ("DAY", "Dayforce, Inc."),
    ("NEXT", "NextDecade Corporation"),
    ("AM", "Antero Midstream Corporation"),
    ("REAL", "The RealReal, Inc."),
    ("CASH", "Pathward Financial, Inc."),
    ("TWO", "Two Harbors Investment Corp"),
    ("WAY", "Waystar Holding Corp"),
]
QA_SYMBOLS = {symbol for symbol, _ in QA_TEN}

#: Realistic Reddit/X-style prose using ALL TEN symbols as ordinary English.
#: Three registers, because each exercises a different resolver path: plain
#: lowercase prose (should not even produce candidates), sentence-initial
#: capitalised forms (capitalisation there is uninformative), and ALL-CAPS
#: shouting posts dense with finance vocabulary (every word becomes a
#: bare-symbol candidate AND the context bonus is at its maximum -- the
#: hardest case, and exactly the register the junk aggregates came from).
ADVERSARIAL_TEXTS = [
    # Ordinary lowercase prose -- every one of the ten appears.
    "as you can see, an hour into the day i am not sure there is a real way "
    "to know. cash out now or wait two more days for the next move?",
    # Sentence-initial capitalised forms (first-word capitalisation is how
    # English works, not ticker intent).
    "As I said yesterday. You all knew this. An ugly open. Day two of the "
    "slide. Next week could differ. Am I surprised? Real shame. Cash stayed "
    "on the sidelines. Two hours in. Way too early to call it.",
    # ALL-CAPS shouting with heavy finance vocabulary nearby -- the context
    # bonus alone must never carry a common word over the pipeline floor.
    "AS YOU CAN SEE I BOUGHT CALLS AND PUTS THE NEXT DAY AND NOW I AM DOWN "
    "BIG. NO WAY AM I SELLING. TWO MORE RED DAYS AND MY CASH IS GONE. THE "
    "REAL LESSON: AN OPTIONS POSITION IS NOT A PLAN.",
    # Mixed-register rant: sentence-initial caps plus trading vocabulary.
    "AS I keep saying, buy good companies. YOU should know better than to "
    "chase. AN entry here is risky. DAY one of earnings season. NEXT up, "
    "more volume. AM I the only one buying calls? REAL money is made "
    "holding. CASH is a position too. TWO more sessions of selling. WAY too "
    "much hype in here.",
]


def _post(text: str, external_id: str = "p1") -> SocialPost:
    return SocialPost(
        source=SocialSource.REDDIT,
        external_id=external_id,
        created_at=dt.datetime(2026, 7, 31, 14, 0, tzinfo=dt.UTC),
        text=text,
        score=25,
        author_hash="author1",
    )


@pytest.fixture()
def resolver() -> TickerResolver:
    """Directory containing all ten QA securities plus ordinary controls.

    The controls (AAPL/NVDA) prove the negative results below come from the
    ambiguity handling, not from a broken resolver that matches nothing.
    """
    directory = {s: SecurityInfo(symbol=s, name=n) for s, n in QA_TEN}
    directory["AAPL"] = SecurityInfo(symbol="AAPL", name="Apple Inc.")
    directory["NVDA"] = SecurityInfo(symbol="NVDA", name="NVIDIA Corporation")
    return TickerResolver(directory=directory)


class TestQaTenAreKnownAmbiguous:
    """Every one of the ten must take the discounted ambiguous path.

    ``COMMON_WORDS_AND_ACRONYMS`` is generated (wordfreq top-30k); if a
    regeneration ever dropped one of these words, this is the test that says
    so before production trending does.
    """

    @pytest.mark.parametrize("symbol", sorted(QA_SYMBOLS))
    def test_symbol_is_in_the_generated_common_words_set(self, symbol: str):
        assert symbol in COMMON_WORDS_AND_ACRONYMS

    @pytest.mark.parametrize("symbol", sorted(QA_SYMBOLS))
    def test_symbol_is_ambiguous(self, symbol: str):
        assert is_ambiguous_symbol(symbol)

    def test_threshold_under_test_is_the_real_default(self):
        # Pin the operating point: if the default ever moves, this suite must
        # be re-evaluated at the new floor rather than silently testing a
        # threshold production no longer uses.
        assert pytest.approx(0.60) == THRESHOLD


class TestOrdinaryEnglishMintsNothing:
    """The F25 acceptance check: prose -> ZERO stored-quality mentions."""

    @pytest.mark.parametrize("text", ADVERSARIAL_TEXTS)
    def test_no_mention_reaches_the_pipeline_threshold(
        self, resolver: TickerResolver, text: str
    ):
        post = _post(text)
        assert resolver.resolve_filtered(post, THRESHOLD) == []

    def test_zero_mentions_across_the_whole_adversarial_corpus(
        self, resolver: TickerResolver
    ):
        minted: set[str] = set()
        for i, text in enumerate(ADVERSARIAL_TEXTS):
            for mention in resolver.resolve_filtered(_post(text, f"p{i}"), THRESHOLD):
                minted.add(mention.symbol)
        assert minted == set()

    def test_lowercase_prose_produces_no_candidates_at_all(
        self, resolver: TickerResolver
    ):
        """Plain lowercase usage is not merely below threshold -- it is not
        even a candidate: the bare-symbol path only fires on genuinely
        all-caps tokens, and none of the ten is indexed as its own alias
        (ambiguous symbols are excluded from self-aliasing by design)."""
        mentions = resolver.resolve(_post(ADVERSARIAL_TEXTS[0]))
        assert {m.symbol for m in mentions} & QA_SYMBOLS == set()

    def test_shouting_post_candidates_stay_strictly_below_threshold(
        self, resolver: TickerResolver
    ):
        """The all-caps register DOES produce bare-symbol candidates -- and
        every one of them must sit strictly below the pipeline floor even
        with the finance-context bonus saturated."""
        mentions = resolver.resolve(_post(ADVERSARIAL_TEXTS[2]))
        qa_candidates = [m for m in mentions if m.symbol in QA_SYMBOLS]
        assert qa_candidates, "expected bare-symbol candidates from the all-caps post"
        assert all(m.confidence < THRESHOLD for m in qa_candidates)


class TestDiscountedNeverBlocked:
    """Real references to the same ten securities still resolve."""

    def test_uppercase_cashtag_clears_the_threshold(self, resolver: TickerResolver):
        mentions = resolver.resolve_filtered(
            _post("$AS breaking out of the base on volume, adding here"), THRESHOLD
        )
        as_mention = next(m for m in mentions if m.symbol == "AS")
        assert as_mention.method == "cashtag"
        assert as_mention.confidence >= THRESHOLD

    @pytest.mark.parametrize(
        ("text", "symbol"),
        [
            ("Amer Sports crushed earnings, raising guidance again", "AS"),
            ("AutoNation buyback announced, shares up premarket", "AN"),
            ("Two Harbors Investment cut its dividend after the close", "TWO"),
        ],
    )
    def test_company_names_clear_the_threshold(
        self, resolver: TickerResolver, text: str, symbol: str
    ):
        mentions = resolver.resolve_filtered(_post(text), THRESHOLD)
        assert symbol in {m.symbol for m in mentions}

    def test_lowercase_cashtag_earns_the_threshold_from_context(
        self, resolver: TickerResolver
    ):
        """"$cash" could be a stylistic dollar sign, so it starts below the
        cashtag base -- but the "$" plus one or two finance terms nearby is
        real ticker intent and clears the pipeline floor."""
        mentions = resolver.resolve_filtered(
            _post("$cash printing today, buying more calls into earnings"), THRESHOLD
        )
        assert "CASH" in {m.symbol for m in mentions}

    def test_bare_symbol_earns_confidence_from_finance_context(
        self, resolver: TickerResolver
    ):
        """A deliberate bare "AS" callout amid heavy finance vocabulary earns
        materially more confidence than the same token in plain English --
        the "earn it from context" mechanism working -- while remaining
        below the actionable pipeline threshold BY DESIGN. This ceiling is
        the F25 fix itself: social-finance posts supply trading vocabulary
        around *every* word, so context alone must never be able to promote
        a bare common word to stored-mention quality; for these symbols the
        actionable paths are the cashtag and the company name (asserted
        above). Do not "fix" this assertion upward without re-running the
        adversarial corpus in this module."""
        contextual = _post("Adding to my AS position before earnings, breakout volume looks strong")
        plain = _post(ADVERSARIAL_TEXTS[2])

        contextual_conf = {
            m.symbol: m.confidence for m in resolver.resolve(contextual)
        }.get("AS", 0.0)
        plain_conf = {m.symbol: m.confidence for m in resolver.resolve(plain)}.get("AS", 0.0)

        # Earned: clears the resolver's own actionable floor (0.35 -- the
        # convention pinned by test_ticker_resolution.TestSlangCollisionAudit)
        # and beats the ordinary-English reading of the same token...
        assert contextual_conf >= 0.35
        assert contextual_conf > plain_conf
        # ...but stays below the pipeline's stored-mention threshold.
        assert contextual_conf < THRESHOLD

    def test_unambiguous_control_still_resolves_bare(self, resolver: TickerResolver):
        """NVDA proves the suite's negatives are ambiguity-specific: an
        ordinary ticker with the same finance context resolves actionably
        from a bare mention."""
        mentions = resolver.resolve_filtered(
            _post("NVDA earnings next week, I'm long calls into the print"), THRESHOLD
        )
        assert "NVDA" in {m.symbol for m in mentions}


class TestAggregationBullBearSignature:
    """End-to-end guard against the all-neutral regression (QA F25's second
    signature): posts classified by the CURRENT classifier must aggregate to
    ``bull_bear_ratio != 1.0`` whenever directional language is present.
    ``bull_bear_ratio == 1.0`` across the board is the fingerprint of
    pre-b9fe566 rows (all-neutral scores -> ``bull_sum <= 1e-9`` -> 1.0), and
    is exactly what ``sentiment.rebuild`` exists to purge."""

    _SESSION = dt.date(2026, 7, 31)  # a Friday: session-close math stays boring
    _CLOSE = dt.datetime(2026, 7, 31, 20, 0, tzinfo=dt.UTC)

    def _corpus(self, texts: list[str]):
        from claudetrade.domain import TickerMention

        clf = RuleSentimentClassifier()
        posts, mentions, scores = [], [], {}
        for i, text in enumerate(texts):
            post = SocialPost(
                source=SocialSource.REDDIT,
                external_id=f"bb{i}",
                created_at=self._CLOSE - dt.timedelta(hours=2 + i),
                text=text,
                score=20,
                author_hash=f"author{i}",
            )
            mention = TickerMention(
                post_external_id=post.external_id,
                symbol="AMZN",
                confidence=0.92,
                method="cashtag",
                matched_text="$AMZN",
                context=post.text,
            )
            posts.append(post)
            mentions.append(mention)
            scores[post.external_id] = clf.classify(post, "AMZN", [mention])
        return posts, mentions, scores

    def _aggregate(self, texts: list[str]):
        from claudetrade.sentiment.aggregation import SentimentAggregator

        posts, mentions, scores = self._corpus(texts)
        return SentimentAggregator(SentimentConfig()).aggregate(
            "AMZN", self._SESSION, posts, mentions, scores
        )

    def test_bullish_corpus_reads_bullish_not_neutral(self):
        snap = self._aggregate(
            [
                "$AMZN crushed earnings, very bullish. Buying more calls!",
                "$AMZN beat expectations, guidance raised. Loading up here.",
                "Great earnings from $AMZN, this is going higher.",
                "$AMZN to the moon after that blowout quarter.",
                "$AMZN printing money, growth is back. Long term hold.",
            ]
        )
        assert snap.post_count == 5
        assert snap.bull_bear_ratio != pytest.approx(1.0)
        assert snap.bull_bear_ratio > 1.0

    def test_mixed_corpus_with_a_bullish_tilt_is_not_flat(self):
        snap = self._aggregate(
            [
                "$AMZN crushed earnings, very bullish. Buying calls.",
                "$AMZN guidance raised, going higher from here.",
                "$AMZN breaking out on strong volume, loading up.",
                "$AMZN missed on margins, I'm selling. Bearish.",
            ]
        )
        assert snap.bull_bear_ratio != pytest.approx(1.0)
        assert snap.bull_bear_ratio > 1.0

    def test_all_neutral_scores_still_produce_the_legacy_signature(self):
        """Documents WHY ratio==1.0 flags stale rows: with zero bullish and
        zero bearish mass the aggregator (deliberately) reports exactly 1.0
        -- so a board full of 1.0s means the classifier never scored the
        posts, which after b9fe566 only stored pre-fix rows can exhibit."""
        from claudetrade.domain import SentimentScores
        from claudetrade.sentiment.aggregation import SentimentAggregator

        posts, mentions, _ = self._corpus(["$AMZN report due tuesday", "$AMZN files 10-K"])
        neutral_scores = {p.external_id: SentimentScores(neutral=1.0) for p in posts}
        snap = SentimentAggregator(SentimentConfig()).aggregate(
            "AMZN", self._SESSION, posts, mentions, neutral_scores
        )
        assert snap.bull_bear_ratio == pytest.approx(1.0)
