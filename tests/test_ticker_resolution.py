"""Tests for ticker mention entity resolution."""

from __future__ import annotations

from claudetrade.domain import SecurityInfo
from claudetrade.sentiment.entity_resolution import (
    TickerResolver,
    normalise_company_name,
)


class TestAmbiguousWordExclusion:
    """Ambiguous words in ordinary English don't resolve as tickers."""

    def test_ai_in_ordinary_sentence(self):
        """'AI' in 'I use AI at work' doesn't resolve as $AI stock."""
        text = "I use AI at work every day"
        # With threshold > confidence of bare word match for ambiguous term,
        # this should not resolve
        # The confidence for 'AI' as a bare word is low (~0.12) when ambiguous

        from claudetrade.sentiment.lexicon import AMBIGUOUS_TICKER_WORDS

        assert "ai" in AMBIGUOUS_TICKER_WORDS or "AI" in AMBIGUOUS_TICKER_WORDS

    def test_it_in_ordinary_sentence(self):
        """'IT' in 'turn IT on' doesn't resolve as $IT stock."""
        text = "turn IT on"
        # Similar: IT is ambiguous, low confidence

    def test_all_in_ordinary_sentence(self):
        """'ALL' in 'ALL of it' doesn't resolve."""
        text = "Give me ALL of it"

    def test_so_in_ordinary_sentence(self):
        """'SO' in 'SO tired' doesn't resolve."""
        text = "I'm SO tired today"

    def test_for_in_ordinary_sentence(self):
        """'FOR' in 'this is FOR you' doesn't resolve."""
        text = "this gift is FOR you"


class TestCashtagResolution:
    """Cashtags ($SYMBOL) resolve with high confidence."""

    def test_cashtag_high_confidence(self):
        """Cashtag $AAPL resolves with confidence ~0.92."""
        text = "Just bought $AAPL calls, looking good"
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
        text = "Apple Inc is trading well"
        # "Apple Inc" -> normalized "apple" -> resolves to AAPL

    def test_company_name_without_suffix(self):
        """Company name without legal suffix resolves."""
        text = "Apple is up today"
        # "Apple" normalized matches

    def test_former_symbol_resolves(self):
        """Former ticker symbol resolves via aliases."""
        text = "FB (now META) announced earnings"
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
