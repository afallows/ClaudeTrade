"""Sanitisation and defensive handling of untrusted external text.

All Reddit and X content is treated as **untrusted data, never as instructions**.
Three things happen before any social post is stored or shown to a model:

1. **Minimisation** -- usernames, emails, URLs and phone-like strings are
   stripped or replaced with placeholders, so personal data does not leave the
   machine (see ``docs/security-and-privacy.md``).
2. **Neutralisation** -- sequences that imitate chat-template control tokens or
   that read as instructions to an assistant are defanged, so a post cannot
   hijack a downstream classification prompt.
3. **Delimiting** -- the caller wraps the result in an explicit data fence and
   tells the model, in the system prompt, that fenced content is data.

None of this is a guarantee. Prompt injection is not a solved problem, so the
architecture also assumes the model's output is untrusted: AI results are
schema-validated and can only ever *lower* confidence or add commentary. They
can never widen a stop, raise a position size, or bypass a risk limit.
"""

from __future__ import annotations

import html
import re
import unicodedata

# --- patterns -------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_USER_RE = re.compile(r"(?:^|\s)(?:/?u/|@)([A-Za-z0-9_\-]{2,30})\b")
_SUBREDDIT_RE = re.compile(r"\b/?r/([A-Za-z0-9_]{2,30})\b")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_WS_RE = re.compile(r"\s+")

#: Tokens that imitate chat-template control markers. Broken with a zero-width
#: -free visible marker so the literal sequence cannot survive into a prompt.
_CONTROL_TOKEN_RE = re.compile(
    r"(<\|[^>]{0,40}\|>"  # <|im_start|>, <|endoftext|>, ...
    r"|\[/?INST\]"  # llama-style instruction tags
    r"|<<\s*/?SYS\s*>>"
    r"|\bHuman:\s"
    r"|\bAssistant:\s"
    r"|\bSystem:\s"
    r"|</?system>"
    r"|</?instructions?>"
    r"|```+\s*system)",
    re.IGNORECASE,
)

#: Phrases whose only purpose in a social post is to redirect an LLM.
_INJECTION_PHRASES = [
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions?|prompts?|rules?)",
    r"disregard (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions?|prompts?|rules?)",
    r"forget (?:everything|all)(?: you were told)?",
    r"you are now (?:a|an|in) \w+",
    r"new (?:system )?(?:instructions?|prompt|rules?)\s*[:\-]",
    r"act as (?:a|an) (?:different|new|unrestricted)",
    r"(?:developer|debug|god|admin|jailbreak) mode",
    r"reveal (?:your|the) (?:system )?(?:prompt|instructions?)",
    r"print (?:your|the) (?:system )?(?:prompt|instructions?)",
    r"do not follow (?:your|the) (?:rules?|guidelines?|instructions?)",
    r"override (?:the )?(?:risk|safety|stop)[- ]?(?:limits?|controls?|loss)",
    r"execute (?:the following|this) (?:code|command|shell)",
    r"run (?:the following|this) (?:code|command|script)",
    r"buy (?:it )?(?:now|immediately) (?:without|regardless of)",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PHRASES), re.IGNORECASE)

#: Redaction placeholders, kept short so they cost few tokens.
URL_PLACEHOLDER = "[url]"
USER_PLACEHOLDER = "[user]"
EMAIL_PLACEHOLDER = "[email]"
PHONE_PLACEHOLDER = "[phone]"
NEUTRALISED = "[filtered]"

MAX_AI_CHARS = 1200


def strip_control_chars(text: str) -> str:
    """Remove zero-width, bidi-override and other invisible control characters.

    Bidirectional overrides and zero-width joiners are a known way to hide text
    from a human reviewer while keeping it visible to a tokeniser.
    """
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in {"Cf", "Cc"} and ch not in "\n\t":
            continue
        out.append(ch)
    return "".join(out)


def normalise_whitespace(text: str) -> str:
    """Collapse runs of whitespace to single spaces and trim."""
    return _WS_RE.sub(" ", text).strip()


def sanitize_social_text(
    text: str,
    *,
    keep_subreddit: bool = True,
    max_chars: int | None = None,
) -> str:
    """Produce a storable/model-safe version of an untrusted social post.

    Args:
        text: Raw post or comment body.
        keep_subreddit: Retain ``r/<name>`` tokens (community is a useful signal
            and is not personal data); usernames are always removed.
        max_chars: Optional truncation applied last.

    Returns:
        Sanitised text with personal identifiers replaced by placeholders and
        instruction-like sequences neutralised.
    """
    if not text:
        return ""
    out = html.unescape(text)
    out = unicodedata.normalize("NFKC", out)
    out = strip_control_chars(out)

    out = _URL_RE.sub(URL_PLACEHOLDER, out)
    out = _EMAIL_RE.sub(EMAIL_PLACEHOLDER, out)
    out = _PHONE_RE.sub(PHONE_PLACEHOLDER, out)
    if keep_subreddit:
        out = _SUBREDDIT_RE.sub(lambda m: f"r/{m.group(1)}", out)
    else:
        out = _SUBREDDIT_RE.sub("", out)
    out = _USER_RE.sub(f" {USER_PLACEHOLDER}", out)

    out = _CONTROL_TOKEN_RE.sub(NEUTRALISED, out)
    out = _INJECTION_RE.sub(NEUTRALISED, out)

    out = normalise_whitespace(out)
    if max_chars is not None and len(out) > max_chars:
        out = out[:max_chars].rstrip() + "..."
    return out


def injection_risk_score(text: str) -> float:
    """Heuristic 0-1 score for how much a post looks like a prompt-injection attempt.

    Used to (a) flag the post in the data-quality log and (b) drop it from AI
    classification entirely above the configured threshold.
    """
    if not text:
        return 0.0
    hits = len(_INJECTION_RE.findall(text)) + len(_CONTROL_TOKEN_RE.findall(text))
    if hits == 0:
        return 0.0
    return min(1.0, 0.4 + 0.2 * hits)


def contains_injection_markers(text: str) -> bool:
    """True when the text contains recognised injection or control sequences."""
    return bool(_INJECTION_RE.search(text) or _CONTROL_TOKEN_RE.search(text))


def fence_untrusted(text: str, *, label: str = "SOCIAL_POST") -> str:
    """Wrap sanitised content in an explicit, non-guessable data fence.

    The fence label is echoed in the system prompt so the model is told, in
    band, that everything between the markers is *data to be classified* and
    must not be followed as instruction.
    """
    safe = text.replace(f"<<<{label}", "").replace(f"{label}>>>", "")
    return f"<<<{label}\n{safe}\n{label}>>>"


def prepare_for_ai(text: str, *, max_chars: int = MAX_AI_CHARS) -> str:
    """Full outbound pipeline: sanitise, truncate, then fence."""
    return fence_untrusted(sanitize_social_text(text, max_chars=max_chars))


_CSV_INJECTION_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def sanitize_for_export(value: object) -> object:
    """Defuse spreadsheet formula injection in exported CSV/Excel cells.

    A cell beginning ``=cmd|...`` is executed by some spreadsheet applications
    on open. Any string starting with a formula trigger is prefixed with an
    apostrophe so it is treated as literal text.
    """
    if not isinstance(value, str):
        return value
    if value.startswith(_CSV_INJECTION_PREFIX):
        return "'" + value
    return value


def truncate(text: str, limit: int, suffix: str = "...") -> str:
    """Trim ``text`` to ``limit`` characters with an ellipsis suffix."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix
