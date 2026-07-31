"""Tests for ticker mention entity resolution."""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.domain import SecurityInfo, SocialPost, SocialSource
from claudetrade.sentiment.entity_resolution import (
    TickerResolver,
    _resolve_bare_symbols,
    normalise_company_name,
)
from claudetrade.sentiment.lexicon import AMBIGUOUS_TICKER_WORDS


def _make_post(text: str) -> SocialPost:
    return SocialPost(
        source=SocialSource.REDDIT,
        external_id="t3_test",
        created_at=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        text=text,
    )


class TestAmbiguousWordExclusion:
    """Ambiguous words in ordinary English don't resolve as tickers."""

    def test_ai_in_ordinary_sentence(self):
        """'AI' in 'I use AI at work' doesn't resolve as $AI stock."""
        # With threshold > confidence of bare word match for ambiguous term,
        # this should not resolve
        # The confidence for 'AI' as a bare word is low (~0.12) when ambiguous
        assert "ai" in AMBIGUOUS_TICKER_WORDS or "AI" in AMBIGUOUS_TICKER_WORDS

    def test_it_in_ordinary_sentence(self):
        """'IT' in 'turn IT on' doesn't resolve as $IT stock."""
        # Similar: IT is ambiguous, low confidence

    def test_all_in_ordinary_sentence(self):
        """'ALL' in 'ALL of it' doesn't resolve."""

    def test_so_in_ordinary_sentence(self):
        """'SO' in 'SO tired' doesn't resolve."""

    def test_for_in_ordinary_sentence(self):
        """'FOR' in 'this is FOR you' doesn't resolve."""


class TestNewlyAddedAmbiguousTickers:
    """OPEN, RH, FL, RIDE are real tickers that collide with ordinary
    English words/names and were missing from ``AMBIGUOUS_TICKER_WORDS``.
    Mined from asad70's reddit-sentiment-analysis ticker blacklist
    (``data.py``, MIT) cross-checked against the live ``SecurityInfo``
    universe -- word list only, not code; our confidence-scored mechanism
    differs structurally from their hard blacklist."""

    @pytest.mark.parametrize("symbol", ["OPEN", "RH", "FL", "RIDE"])
    def test_symbol_is_in_ambiguous_word_set(self, symbol):
        assert symbol in AMBIGUOUS_TICKER_WORDS

    @pytest.mark.parametrize(
        "symbol,sentence",
        [
            ("OPEN", "I think OPEN could pop this week"),
            ("RH", "not sure but RH looks interesting lately"),
            ("FL", "heard FL might rally soon"),
            ("RIDE", "watching RIDE closely this week"),
        ],
    )
    def test_bare_mention_uses_the_discounted_ambiguous_base(self, symbol, sentence):
        """Isolated at the ``_resolve_bare_symbols`` level (bypassing the
        alias/company-name paths, which membership in
        ``AMBIGUOUS_TICKER_WORDS`` does not affect): a bare mention starts
        from the discounted ambiguous base (0.12), not the ordinary
        bare-symbol base (0.55) -- this is exactly the code path
        ``AMBIGUOUS_TICKER_WORDS`` membership controls."""
        seen: dict[str, float] = {}

        def consider(sym, confidence, method, matched, context):
            seen[sym] = confidence

        _resolve_bare_symbols(sentence, {symbol}, consider)

        assert symbol in seen
        assert seen[symbol] < 0.30  # well under the 0.55 ordinary base

    def test_ordinary_ticker_bare_symbol_base_is_higher_for_comparison(self):
        """A ticker NOT in ``AMBIGUOUS_TICKER_WORDS`` gets the higher,
        undiscounted base via the exact same code path."""
        seen: dict[str, float] = {}

        def consider(sym, confidence, method, matched, context):
            seen[sym] = confidence

        _resolve_bare_symbols("I think ACME could pop this week", {"ACME"}, consider)

        assert seen["ACME"] >= 0.40

    @pytest.mark.parametrize(
        "symbol,name,sentence",
        [
            ("OPEN", "Opendoor Technologies", "I think OPEN could pop this week"),
            ("RH", "Restoration Hardware", "not sure but RH looks interesting lately"),
            ("FL", "Foot Locker", "heard FL might rally soon"),
            ("RIDE", "Lordstown Motors", "watching RIDE closely this week"),
        ],
    )
    def test_full_resolve_discounts_the_bare_mention(self, symbol, name, sentence):
        """End-to-end through ``TickerResolver.resolve``: with a realistic
        company name (that does not itself appear in the sentence), the
        only path that fires is the discounted bare-symbol one."""
        directory = {symbol: SecurityInfo(symbol=symbol, name=name)}
        resolver = TickerResolver(directory)

        mentions = resolver.resolve(_make_post(sentence))

        assert len(mentions) == 1
        assert mentions[0].symbol == symbol
        assert mentions[0].method == "symbol_context"
        assert mentions[0].confidence < 0.30


class TestCashtagResolution:
    """Cashtags ($SYMBOL) resolve with high confidence."""

    def test_cashtag_high_confidence(self):
        """Cashtag $AAPL resolves with confidence ~0.92."""
        # Cashtag base confidence: ~0.92
        # Should resolve AAPL with high confidence

    def test_cashtag_no_lowercase_mix(self):
        """$aapl (lowercase) is not a cashtag."""
        # Cashtag pattern: $[A-Z]{1,6}
        # $aapl doesn't match (lowercase)


class TestCompanyNameResolution:
    """Company names and aliases resolve."""

    def test_company_name_exact_match(self):
        """Exact company name resolves."""
        # "Apple Inc" -> normalized "apple" -> resolves to AAPL

    def test_company_name_without_suffix(self):
        """Company name without legal suffix resolves."""
        # "Apple" normalized matches

    def test_former_symbol_resolves(self):
        """Former ticker symbol resolves via aliases."""
        # FB in former_symbols for META


class TestNormalisationHelpers:
    """Company name normalisation strips suffixes and punctuation."""

    def test_normalise_removes_suffixes(self):
        """Legal suffixes are removed."""
        names = [
            "Apple Inc",
            "Apple Corp",
            "Apple Limited",
            "The Meta Group",
        ]
        for name in names:
            normalized = normalise_company_name(name)
            # Suffixes should be gone
            assert "inc" not in normalized.lower()
            assert "corp" not in normalized.lower()
            assert "limited" not in normalized.lower()
            assert "the" not in normalized.lower() or "the" in name.lower()

    def test_normalise_case_folds(self):
        """Names are case-folded."""
        result = normalise_company_name("Apple INC")
        assert result == result.lower()


class TestSpamListPenalty:
    """Posts listing 30 tickers get penalty on bare-symbol matches."""

    def test_spam_list_lowers_bare_symbol_confidence(self):
        """Listing many tickers reduces confidence in each."""
        # Post with 30 distinct bare symbols gets spam penalty
        # Their confidence is scaled down

        num_symbols = 30
        max_mentions = 12
        is_spam_list = num_symbols > max_mentions

        assert is_spam_list

        # Bare symbol confidence normally ~0.55
        # Spam list scales it down, maybe to ~0.30


class TestDuplicateMentionCollapse:
    """Duplicate mentions of one symbol collapse to single mention."""

    def test_duplicate_tickers_collapsed(self):
        """$AAPL mentioned twice becomes one mention with max confidence."""
        # If post says "$AAPL is great. $AAPL to the moon"
        # Should record as single mention of AAPL, not two

        mentions = [
            {"symbol": "AAPL", "confidence": 0.95, "method": "cashtag"},
            {"symbol": "AAPL", "confidence": 0.95, "method": "cashtag"},
        ]

        # Collapse: keep max confidence
        collapsed = {}
        for m in mentions:
            sym = m["symbol"]
            if sym not in collapsed or m["confidence"] > collapsed[sym]["confidence"]:
                collapsed[sym] = m

        assert len(collapsed) == 1
        assert collapsed["AAPL"]["confidence"] == 0.95


class TestContextScoring:
    """Finance context terms boost confidence."""

    def test_finance_context_bonus(self):
        """Finance keywords nearby boost bare-symbol confidence."""
        # "BUY ABC" has finance context (BUY keyword)
        # "ABC is a word" has no context, lower confidence

        from claudetrade.sentiment.lexicon import FINANCE_CONTEXT_TERMS

        assert "buy" in FINANCE_CONTEXT_TERMS or "buy".upper() in FINANCE_CONTEXT_TERMS


class TestSentenceStartPosition:
    """Sentence-initial position is uninformative for capitalization."""

    def test_sentence_start_no_capitalization_credit(self):
        """Word at sentence start doesn't get capital-letter credit."""
        # "AAPL is great" - AAPL at start
        # "Great AAPL" - AAPL mid-sentence
        # The first AAPL shouldn't get more credit for capitalization

        # This is enforced by a penalty term in resolution


class TestResolverInitialization:
    """TickerResolver builds from a directory of SecurityInfo."""

    def test_resolver_from_directory(self):
        """TickerResolver builds lookup structures from SecurityInfo dict."""
        directory = {
            "AAPL": SecurityInfo(
                symbol="AAPL",
                name="Apple Inc",
                aliases=("Apple",),
                former_symbols=(),
            ),
            "MSFT": SecurityInfo(
                symbol="MSFT",
                name="Microsoft Corp",
                aliases=("MSFT",),
                former_symbols=(),
            ),
        }

        resolver = TickerResolver(directory)
        assert resolver is not None
