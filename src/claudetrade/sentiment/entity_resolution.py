"""Resolve ticker mentions inside untrusted social-media text.

This is the highest-leverage component in the sentiment pipeline: get it wrong
and every downstream number is measuring the wrong symbol (or measuring noise
about the English word "IT" instead of the stock ``IT``). The resolver never
claims certainty -- every candidate mention carries a ``confidence`` in
``[0, 1]`` and a ``method`` describing which evidence produced it, and callers
decide (via ``resolve_filtered`` or their own threshold) how much evidence is
enough for their purpose.

Evidence combined, from strongest to weakest:

1. **Cashtag** (``$AAPL``) -- an explicit, unambiguous ticker marker.
2. **Company name / alias / former-symbol match** -- also strong, after
   normalising away legal suffixes (Inc, Corp, Ltd, plc, ...) and case.
3. **Bare uppercase symbol** against the known symbol list -- moderate at
   best, and cut sharply for words in ``lexicon.AMBIGUOUS_TICKER_WORDS``.
4. **Finance-context terms nearby** raise confidence; their absence, combined
   with ordinary lowercase prose around the token, lowers it.
5. **Sentence-initial position** is treated as *uninformative* for
   capitalisation: English capitalises the first word of a sentence
   regardless of whether it is a ticker, so a token seen only there gets no
   credit for looking "shouty".
6. **Spam signals** (an implausible number of distinct symbols in one post)
   scale down the low-confidence bare-symbol matches -- a list of 40 tickers
   is a spam blast, not forty considered opinions.

**Honest limitation**: this is still a heuristic scanner, not an entity
linker with real disambiguation (no WSD model, no knowledge graph). Rare or
newly listed symbols that collide with common words and lack surrounding
finance vocabulary will sometimes be missed entirely, and colloquial
tickers ("doge" for a meme coin, informal abbreviations) are out of scope.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from claudetrade.domain import SecurityInfo, SocialPost, TickerMention
from claudetrade.sentiment.lexicon import AMBIGUOUS_TICKER_WORDS, FINANCE_CONTEXT_TERMS

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------

#: Legal-entity suffixes stripped before comparing company names. Order
#: matters only for readability; matching is whole-word so overlaps are safe.
_LEGAL_SUFFIXES = (
    "incorporated",
    "corporation",
    "corp",
    "inc",
    "ltd",
    "limited",
    "plc",
    "co",
    "company",
    "holdings",
    "holding",
    "group",
    "the",
)
_SUFFIX_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(s) for s in _LEGAL_SUFFIXES) + r")\b\.?", re.IGNORECASE
)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,6})\b")
#: Bare uppercase word candidates: 1-5 letters, no lowercase mixed in.
_BARE_SYMBOL_RE = re.compile(r"\b[A-Z]{1,5}\b")
_TOKEN_RE = re.compile(r"[A-Za-z']+")
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?]\s+")

#: Beyond this many distinct candidate symbols, a post reads as a spam ticker
#: list rather than a considered opinion about each name.
DEFAULT_MAX_MENTIONS_PER_POST = 12
#: How many tokens either side of a bare-symbol candidate count as "nearby"
#: when looking for finance-context vocabulary.
_CONTEXT_WINDOW_TOKENS = 6

# Confidence constants. Tuned by hand against the verification corpus rather
# than fitted -- treat as a starting point, not a calibrated probability.
_CASHTAG_BASE = 0.92
_NAME_BASE = 0.85
_ALIAS_BASE = 0.8
_BARE_BASE_ORDINARY = 0.55
_BARE_BASE_AMBIGUOUS = 0.12
_CONTEXT_BONUS_PER_HIT = 0.12
_MAX_CONTEXT_HITS_COUNTED = 3
_SENTENCE_START_PENALTY = 0.12
_LOWERCASE_RUN_PENALTY = 0.12

#: Signature for the per-candidate callback threaded through resolution: takes
#: (symbol, confidence, method, matched_text, context) and records the best
#: candidate seen so far for that symbol.
_ConsiderFn = Callable[[str, float, str, str, str], None]


def normalise_company_name(name: str) -> str:
    """Case-fold a company name/alias and strip legal suffixes and punctuation.

    ``"Meta Platforms, Inc."`` and ``"meta platforms"`` must compare equal, and
    so must ``"Facebook Inc"`` (a former name) once aliases are indexed.
    """
    out = name.casefold()
    out = _SUFFIX_RE.sub(" ", out)
    out = _PUNCT_RE.sub(" ", out)
    out = _WS_RE.sub(" ", out).strip()
    return out


def _strip_apostrophes(word: str) -> str:
    return word.replace("'", "").replace("\u2019", "")


@dataclass(slots=True)
class _AliasEntry:
    symbol: str
    method: str  # "company_name" | "alias"


@dataclass(slots=True)
class TickerResolver:
    """Resolves ticker mentions in post text against a known symbol universe.

    Args:
        directory: ``symbol -> SecurityInfo`` for every security eligible to
            be matched. Only symbols present here can ever be resolved --
            an unknown all-caps word is just a word.
        extra_aliases: Additional ``normalised_alias -> symbol`` mappings not
            already implied by ``SecurityInfo.aliases``/``former_symbols``
            (e.g. hand-curated nicknames). Optional.
        max_mentions_per_post: Distinct symbols above this count trigger the
            spam-list penalty on bare-symbol (non-cashtag) matches.
    """

    directory: dict[str, SecurityInfo]
    extra_aliases: dict[str, str] = field(default_factory=dict)
    max_mentions_per_post: int = DEFAULT_MAX_MENTIONS_PER_POST
    #: Derived lookup structures, built in __post_init__. They must be declared
    #: as fields because this is a slots dataclass -- a slots class cannot gain
    #: attributes that were never declared.
    _symbols: set[str] = field(default_factory=set, init=False, repr=False)
    _alias_index: dict[str, _AliasEntry] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._symbols = set(self.directory.keys())
        self._alias_index = {}
        for symbol, info in self.directory.items():
            self._index_alias(normalise_company_name(symbol), symbol, "alias")
            if info.name:
                self._index_alias(normalise_company_name(info.name), symbol, "company_name")
            for alias in info.aliases:
                self._index_alias(normalise_company_name(alias), symbol, "alias")
            for former in info.former_symbols:
                self._index_alias(normalise_company_name(former), symbol, "alias")
        for alias, symbol in self.extra_aliases.items():
            self._index_alias(normalise_company_name(alias), symbol, "alias")

    def _index_alias(self, normalised: str, symbol: str, method: str) -> None:
        if not normalised or len(normalised) < 2:
            return
        existing = self._alias_index.get(normalised)
        # First registration wins; company_name should not be clobbered by a
        # later, weaker alias of a different symbol sharing the same text.
        if existing is None:
            self._alias_index[normalised] = _AliasEntry(symbol=symbol, method=method)
        elif existing.symbol != symbol:
            log.debug(
                "alias collision: %r already maps to %s, ignoring duplicate for %s",
                normalised,
                existing.symbol,
                symbol,
            )

    # -- public API ---------------------------------------------------------

    def resolve(self, post: SocialPost) -> list[TickerMention]:
        """All candidate mentions in ``post``, deduped to one per symbol.

        Returns every candidate regardless of confidence; callers filter (see
        :meth:`resolve_filtered`) so the threshold decision stays with the
        caller rather than being baked into resolution.
        """
        text = post.text or ""
        best: dict[str, TickerMention] = {}

        def _consider(symbol: str, confidence: float, method: str, matched: str, context: str) -> None:
            if symbol not in self._symbols:
                return
            confidence = max(0.0, min(1.0, confidence))
            current = best.get(symbol)
            if current is None or confidence > current.confidence:
                best[symbol] = TickerMention(
                    post_external_id=post.external_id,
                    symbol=symbol,
                    confidence=confidence,
                    method=method,
                    matched_text=matched,
                    context=context,
                )

        for match in _CASHTAG_RE.finditer(text):
            symbol = match.group(1).upper()
            if symbol in self._symbols:
                ctx = _window_text(text, match.start(), match.end())
                _consider(symbol, _CASHTAG_BASE, "cashtag", match.group(0), ctx)

        normalised_text = normalise_company_name(text)
        for alias, entry in self._alias_index.items():
            # Whole-phrase containment on normalised text; word-boundary
            # guarded via padding so "on" doesn't match inside "iron".
            if f" {alias} " in f" {normalised_text} ":
                base = _NAME_BASE if entry.method == "company_name" else _ALIAS_BASE
                _consider(entry.symbol, base, entry.method, alias, alias)

        _resolve_bare_symbols(text, self._symbols, _consider)

        mentions = list(best.values())
        return _apply_spam_penalty(mentions, self.max_mentions_per_post)

    def resolve_filtered(self, post: SocialPost, min_confidence: float) -> list[TickerMention]:
        """Mentions at or above ``min_confidence`` only."""
        return [m for m in self.resolve(post) if m.confidence >= min_confidence]

    def resolve_batch(self, posts: list[SocialPost]) -> dict[str, list[TickerMention]]:
        """Resolve many posts, keyed by ``post.external_id``."""
        return {post.external_id: self.resolve(post) for post in posts}


def _window_text(text: str, start: int, end: int, radius: int = 40) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def _resolve_bare_symbols(text: str, symbols: set[str], consider: _ConsiderFn) -> None:
    """Scan for bare uppercase tokens that match the known symbol universe."""
    tokens = list(_TOKEN_RE.finditer(text))
    lowered_flags = [t.group(0).islower() for t in tokens]
    sentence_starts = _sentence_start_positions(text, tokens)

    for idx, match in enumerate(tokens):
        raw = match.group(0)
        candidate = _strip_apostrophes(raw).upper()
        if candidate != raw or not _BARE_SYMBOL_RE.fullmatch(raw):
            # Only truly all-caps tokens (no case-fold needed) count as bare
            # symbols -- "ai" or "Ai" is not "AI" written as a ticker.
            continue
        if candidate not in symbols:
            continue

        ambiguous = candidate in AMBIGUOUS_TICKER_WORDS
        confidence = _BARE_BASE_AMBIGUOUS if ambiguous else _BARE_BASE_ORDINARY

        context_hits = _count_context_hits(tokens, idx)
        confidence += _CONTEXT_BONUS_PER_HIT * min(context_hits, _MAX_CONTEXT_HITS_COUNTED)

        if idx in sentence_starts:
            # Capitalisation at a sentence boundary is uninformative in
            # English -- every first word is capitalised regardless of
            # whether it is a ticker, so it earns no confidence credit here.
            confidence -= _SENTENCE_START_PENALTY

        if _in_lowercase_run(lowered_flags, idx):
            # Ordinary prose surrounding the token (mostly lowercase words)
            # suggests grammatical use of a common word, not a deliberate,
            # isolated ticker callout.
            confidence -= _LOWERCASE_RUN_PENALTY

        ctx = _window_text(text, match.start(), match.end())
        consider(candidate, confidence, "symbol_context", raw, ctx)


def _sentence_start_positions(text: str, tokens: list[re.Match[str]]) -> set[int]:
    """Indices (into ``tokens``) of tokens that open the text or a sentence."""
    starts: set[int] = set()
    if not tokens:
        return starts
    starts.add(0)
    boundary_ends = [m.end() for m in _SENTENCE_BOUNDARY_RE.finditer(text)]
    for idx, tok in enumerate(tokens):
        if any(abs(tok.start() - end) <= 1 for end in boundary_ends):
            starts.add(idx)
    return starts


def _count_context_hits(tokens: list[re.Match[str]], idx: int) -> int:
    lo = max(0, idx - _CONTEXT_WINDOW_TOKENS)
    hi = min(len(tokens), idx + _CONTEXT_WINDOW_TOKENS + 1)
    window_words = {tokens[i].group(0).casefold() for i in range(lo, hi) if i != idx}
    return sum(1 for term in FINANCE_CONTEXT_TERMS if " " not in term and term in window_words)


def _in_lowercase_run(lowered_flags: list[str], idx: int, span: int = 4) -> bool:
    lo = max(0, idx - span)
    hi = min(len(lowered_flags), idx + span + 1)
    neighbours = [lowered_flags[i] for i in range(lo, hi) if i != idx]
    if not neighbours:
        return False
    return (sum(neighbours) / len(neighbours)) >= 0.7


def _apply_spam_penalty(mentions: list[TickerMention], cap: int) -> list[TickerMention]:
    """Scale down bare-symbol confidence when a post names an implausible
    number of distinct tickers -- a 40-symbol list is a spam blast, not
    forty separate considered opinions."""
    if len(mentions) <= cap:
        return mentions
    penalty = cap / len(mentions)
    out = []
    for m in mentions:
        if m.method == "symbol_context":
            out.append(
                TickerMention(
                    post_external_id=m.post_external_id,
                    symbol=m.symbol,
                    confidence=max(0.0, m.confidence * penalty),
                    method=m.method,
                    matched_text=m.matched_text,
                    context=m.context,
                )
            )
        else:
            out.append(m)
    return out
