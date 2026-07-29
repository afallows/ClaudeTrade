"""Curated finance-domain lexicons for the deterministic sentiment classifier.

Every collection here is a plain module constant -- ``frozenset`` for yes/no
membership, ``dict[str, float]`` when a term should contribute a graded weight
rather than a fixed hit. Multi-word phrases are matched as substrings against
normalised (lower-cased, whitespace-collapsed) text; single words are matched
against tokens.

**Honest limitation**: this is a bag-of-words/phrase lexicon. It has no parse
tree and cannot resolve scope correctly in general -- "not bullish but not
bearish either" will fire both the bullish and bearish entries, and something
like "I would only be bullish if the Fed cuts, which won't happen" reads as
straightforwardly bullish to a keyword scanner. ``NEGATION_WINDOW`` gives a
crude, local fix (a negator within N tokens *before* a match flips/damps its
contribution) that handles the common short-form case ("not bullish", "no
way this pumps") but not multi-clause negation, sarcasm without a marker word,
or negation that trails its target ("bullish? not with this guidance"). The
rule classifier's ``confidence`` output is deliberately reduced by ambiguity
signals (sarcasm markers, conflicting hits, short text) precisely because this
lexicon cannot be trusted to resolve them on its own -- see
``sentiment.classifiers``.
"""

from __future__ import annotations

#: Tokens before a lexicon match that are checked for a negator or modifier.
#: Kept short deliberately: a negator four words back ("not the best entry but
#: still bullish") usually no longer scopes over the match, and widening the
#: window trades false negatives for false positives without a real parser.
NEGATION_WINDOW = 3

# --------------------------------------------------------------------------
# Directional sentiment
# --------------------------------------------------------------------------

BULLISH_TERMS: dict[str, float] = {
    "bullish": 0.85,
    "buy the dip": 0.75,
    "buying more": 0.6,
    "long term hold": 0.55,
    "undervalued": 0.65,
    "breakout": 0.6,
    "breaking out": 0.6,
    "strong buy": 0.85,
    "accumulating": 0.6,
    "adding to my position": 0.55,
    "great earnings": 0.75,
    "beat expectations": 0.7,
    "crushed earnings": 0.8,
    "raising guidance": 0.7,
    "upgraded": 0.55,
    "price target raised": 0.65,
    "new highs": 0.6,
    "all time high": 0.55,
    "green today": 0.4,
    "rip higher": 0.6,
    "melt up": 0.6,
    "printing money": 0.65,
    "bag secured": 0.5,
    "moon": 0.55,
    "mooning": 0.65,
    "rocket": 0.5,
    "up big": 0.5,
    "steal at these prices": 0.6,
    "best stock": 0.5,
    "loading up": 0.55,
}

BEARISH_TERMS: dict[str, float] = {
    "bearish": 0.85,
    "sell everything": 0.75,
    "selling off": 0.6,
    "dumping": 0.6,
    "overvalued": 0.65,
    "breakdown": 0.6,
    "breaking down": 0.6,
    "strong sell": 0.85,
    "missed expectations": 0.7,
    "missed earnings": 0.75,
    "cut guidance": 0.75,
    "downgraded": 0.55,
    "price target cut": 0.65,
    "new lows": 0.6,
    "all time low": 0.55,
    "red today": 0.4,
    "falling knife": 0.65,
    "dead money": 0.6,
    "bagholders": 0.55,
    "rug pull": 0.75,
    "down big": 0.5,
    "worst stock": 0.5,
    "avoid this stock": 0.6,
    "getting crushed": 0.65,
    "cant catch a bid": 0.55,
}

# --------------------------------------------------------------------------
# Emotional / behavioural registers
# --------------------------------------------------------------------------

HYPE_FOMO_TERMS: dict[str, float] = {
    "fomo": 0.75,
    "yolo": 0.75,
    "all in": 0.7,
    "cant miss this": 0.6,
    "last chance": 0.6,
    "dont miss out": 0.6,
    "everyone is buying": 0.55,
    "going parabolic": 0.7,
    "parabolic": 0.65,
    "explosive move": 0.6,
    "generational opportunity": 0.6,
    "once in a lifetime": 0.6,
    "before it takes off": 0.6,
}

FEAR_TERMS: dict[str, float] = {
    "crash": 0.75,
    "crashing": 0.75,
    "bloodbath": 0.8,
    "panic": 0.7,
    "panic selling": 0.8,
    "selloff": 0.6,
    "carnage": 0.7,
    "scary": 0.5,
    "terrified": 0.65,
    "worried": 0.5,
    "nervous": 0.45,
    "getting wrecked": 0.65,
    "cant sleep": 0.5,
    "margin call": 0.75,
}

CAPITULATION_TERMS: dict[str, float] = {
    "capitulation": 0.85,
    "capitulating": 0.8,
    "im done with this stock": 0.75,
    "im out for good": 0.7,
    "sold everything": 0.75,
    "sold at a loss": 0.65,
    "taking the loss": 0.65,
    "cutting my losses": 0.65,
    "cant take it anymore": 0.7,
    "giving up on this": 0.7,
    "never buying this again": 0.65,
    "final straw": 0.6,
    "washed out": 0.55,
}

UNCERTAINTY_HEDGE_TERMS: dict[str, float] = {
    "not sure": 0.6,
    "no idea": 0.6,
    "who knows": 0.6,
    "could go either way": 0.65,
    "tbd": 0.5,
    "we will see": 0.5,
    "well see": 0.5,
    "i think": 0.35,
    "imo": 0.35,
    "imho": 0.35,
    "possibly": 0.4,
    "maybe": 0.4,
    "hard to say": 0.55,
    "on the fence": 0.55,
    "50 50": 0.55,
    "your guess is as good as mine": 0.6,
}

# --------------------------------------------------------------------------
# Sarcasm and low-signal markers
# --------------------------------------------------------------------------

#: Sarcasm is the hardest register for a lexicon: most of these are explicit
#: markers (the writer signposting irony) rather than sarcasm inferred from
#: tone, which this module cannot detect at all.
SARCASM_MARKERS: dict[str, float] = {
    "/s": 0.9,
    "sure buddy": 0.85,
    "totally not a bubble": 0.85,
    "yeah right": 0.7,
    "as if": 0.5,
    "shocking, i know": 0.7,
    "what could go wrong": 0.65,
    "totally sustainable": 0.6,
    "definitely not a ponzi": 0.8,
    "sure thing": 0.5,
    "wow, genius move": 0.7,
    "great job guys": 0.55,
    "totally fine": 0.5,
    "nothing to see here": 0.6,
}

# --------------------------------------------------------------------------
# Catalyst / narrative categories
# --------------------------------------------------------------------------

EARNINGS_SPECULATION_TERMS: dict[str, float] = {
    "earnings play": 0.7,
    "earnings gamble": 0.75,
    "guidance": 0.4,
    "beat estimates": 0.55,
    "miss estimates": 0.55,
    "eps": 0.35,
    "print earnings": 0.6,
    "earnings call": 0.4,
    "quarterly results": 0.4,
    "revenue beat": 0.5,
    "revenue miss": 0.5,
    "forward guidance": 0.45,
}

PRODUCT_CATALYST_TERMS: dict[str, float] = {
    "product launch": 0.65,
    "new product": 0.5,
    "unveiling": 0.55,
    "keynote": 0.5,
    "ship date": 0.55,
    "release date": 0.5,
    "patent granted": 0.6,
    "patent filed": 0.5,
    "beta launch": 0.5,
    "rolling out": 0.4,
}

REGULATORY_CATALYST_TERMS: dict[str, float] = {
    "fda approval": 0.75,
    "fda rejection": 0.75,
    "sec probe": 0.75,
    "sec investigation": 0.75,
    "lawsuit": 0.6,
    "antitrust": 0.65,
    "investigation": 0.55,
    "regulatory approval": 0.65,
    "regulatory review": 0.5,
    "ban": 0.6,
    "sanctions": 0.6,
    "subpoena": 0.65,
    "class action": 0.6,
}

RUMOUR_MARKERS: dict[str, float] = {
    "heard that": 0.7,
    "sources say": 0.7,
    "unconfirmed": 0.65,
    "rumor has it": 0.7,
    "rumour has it": 0.7,
    "word on the street": 0.65,
    "allegedly": 0.6,
    "according to insiders": 0.65,
    "take this with a grain of salt": 0.6,
    "supposedly": 0.55,
}

SHORT_SQUEEZE_TERMS: dict[str, float] = {
    "short squeeze": 0.8,
    "gamma squeeze": 0.75,
    "short interest": 0.5,
    "squeeze incoming": 0.75,
    "shorts are trapped": 0.75,
    "short covering": 0.65,
    "squeeze play": 0.65,
    "shorts are done": 0.65,
    "days to cover": 0.5,
}

#: Templated language whose *only* purpose is to recruit buyers -- the
#: clearest single lexical signal of pump-and-dump activity.
PUMP_DUMP_TEMPLATES: dict[str, float] = {
    "to the moon": 0.7,
    "load up": 0.75,
    "load up now": 0.8,
    "next 10x": 0.85,
    "next gme": 0.75,
    "get in before": 0.8,
    "back up the truck": 0.7,
    "free money": 0.75,
    "cant lose": 0.75,
    "guaranteed 10x": 0.9,
    "guaranteed money": 0.85,
    "this is the one": 0.5,
    "buy now or regret it": 0.85,
    "dont wait": 0.6,
    "get in now": 0.7,
    "easy money": 0.65,
}

POSITION_DISCLOSURE_PATTERNS: dict[str, float] = {
    "im holding": 0.6,
    "my cost basis": 0.7,
    "sold my": 0.6,
    "i sold": 0.5,
    "i bought": 0.5,
    "added to my position": 0.6,
    "averaging down": 0.6,
    "my position": 0.55,
    "im long": 0.65,
    "im short": 0.65,
    "i own": 0.5,
    "i hold": 0.55,
    "my shares": 0.5,
    "my calls": 0.55,
    "my puts": 0.55,
}

# --------------------------------------------------------------------------
# Negation / modification
# --------------------------------------------------------------------------

NEGATORS: frozenset[str] = frozenset(
    {
        "not",
        "no",
        "never",
        "isnt",
        "arent",
        "wasnt",
        "werent",
        "dont",
        "doesnt",
        "didnt",
        "cannot",
        "cant",
        "wont",
        "wouldnt",
        "without",
        "hardly",
        "barely",
        "none",
        "nobody",
        "neither",
        "nor",
        "aint",
    }
)

#: Multipliers applied to whatever lexicon hit they precede. >1 amplifies,
#: <1 damps. Values are heuristic, not calibrated against labelled data.
INTENSIFIERS: dict[str, float] = {
    "very": 1.4,
    "extremely": 1.6,
    "super": 1.4,
    "massively": 1.6,
    "insanely": 1.6,
    "absolutely": 1.5,
    "totally": 1.3,
    "huge": 1.4,
    "massive": 1.4,
    "incredibly": 1.5,
    "so": 1.2,
    "really": 1.2,
}

DIMINISHERS: dict[str, float] = {
    "slightly": 0.6,
    "somewhat": 0.7,
    "kinda": 0.7,
    "sorta": 0.7,
    "a bit": 0.7,
    "marginally": 0.6,
    "barely": 0.5,
    "mildly": 0.7,
}

# --------------------------------------------------------------------------
# Entity-resolution support
# --------------------------------------------------------------------------

#: Ticker symbols that collide with ordinary English words. A bare, uppercase
#: occurrence of one of these is much weaker evidence of a ticker mention than
#: the same occurrence for e.g. "NVDA" -- see ``sentiment.entity_resolution``.
AMBIGUOUS_TICKER_WORDS: frozenset[str] = frozenset(
    {
        "AI",
        "IT",
        "ON",
        "ALL",
        "FOR",
        "A",
        "ARE",
        "SO",
        "BE",
        "BY",
        "CAN",
        "GO",
        "HAS",
        "HE",
        "IF",
        "IN",
        "IS",
        "ME",
        "NOW",
        "OR",
        "OUT",
        "PM",
        "SEE",
        "TV",
        "UP",
        "US",
        "WE",
        "K",
        "T",
        "U",
        "X",
        "Y",
    }
)

#: Words that, found near a candidate ticker token, make it more likely the
#: token really is being used as a ticker rather than an ordinary word.
FINANCE_CONTEXT_TERMS: frozenset[str] = frozenset(
    {
        "calls",
        "puts",
        "shares",
        "share",
        "position",
        "positions",
        "earnings",
        "price target",
        "market cap",
        "float",
        "er",
        "dd",
        "bagholder",
        "bagholders",
        "strike",
        "options",
        "option",
        "short interest",
        "catalyst",
        "breakout",
        "technicals",
        "chart",
        "resistance",
        "support",
        "volume",
        "dividend",
        "buyback",
        "guidance",
        "ticker",
        "stock",
        "shorting",
        "long",
        "short",
        "iv",
        "premarket",
        "afterhours",
        "atr",
        "rsi",
        "moving average",
    }
)

# --------------------------------------------------------------------------
# Emoji signals
# --------------------------------------------------------------------------

BULLISH_EMOJI: frozenset[str] = frozenset({"\U0001f680", "\U0001f4c8", "\U0001f48e", "\U0001f64c"})
# rocket, chart-up, gem, raised-hands
BEARISH_EMOJI: frozenset[str] = frozenset({"\U0001f4c9", "\U0001f43b", "\U0001f480"})
# chart-down, bear, skull
