"""Design tokens shared by every screen.

One accent colour, a status palette reserved for lifecycle state (never
reused for series identity), and a blue/red diverging pair for direction and
polarity (long vs. short, bullish vs. bearish). Every screen imports its
colours from here instead of hard-coding a hex value, so the app reads as one
system rather than five independently-styled pages.

Palette source: the validated dark-surface palette documented in this
project's `dataviz` design skill (categorical slot 1 = blue, status steps,
diverging blue<->red pair). Values are the dark-surface steps because
`.streamlit/config.toml` fixes `base = "dark"`.
"""

from __future__ import annotations

import streamlit as st

# --- accent ------------------------------------------------------------

#: The single interactive/brand accent used across buttons, links, active
#: nav, chart lines and progress bars.
ACCENT = "#3987e5"
ACCENT_STRONG = "#184f95"

# --- diverging pair: direction / polarity -------------------------------
# Long/short and bullish/bearish are opposite poles of one axis, not
# unrelated categories -- so they get the diverging blue<->red pair rather
# than arbitrary categorical hues.

LONG_COLOR = "#3987e5"  # blue
SHORT_COLOR = "#e66767"  # red
NEUTRAL_COLOR = "#898781"  # muted ink, used when direction is flat/unknown

# --- status palette (fixed; never themed, never reused for a series) ---

STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_SERIOUS = "#ec835a"
STATUS_CRITICAL = "#d03b3b"
STATUS_MUTED = "#898781"

# --- chart chrome (dark surface) ----------------------------------------

SURFACE = "#1a1a19"
PAGE_PLANE = "#0d0d0d"
INK_PRIMARY = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"
GRIDLINE = "#2c2c2a"

#: Sequential blue ramp (light->dark), for magnitude encodings (score bars,
#: heat/exposure). Index 0 is the palest step.
SEQUENTIAL_BLUE = [
    "#cde2fb",
    "#9ec5f4",
    "#6da7ec",
    "#3987e5",
    "#256abf",
    "#184f95",
    "#0d366b",
]

PLOTLY_TEMPLATE = "plotly_dark"

#: Signal lifecycle status -> (icon, colour, display label). Kept here so
#: every screen renders a given status identically.
_STATUS_STYLE: dict[str, tuple[str, str, str]] = {
    "actionable": ("\U0001f7e2", STATUS_GOOD, "Actionable"),
    "approaching": ("\U0001f7e1", STATUS_WARNING, "Approaching"),
    "extended": ("\U0001f7e0", STATUS_SERIOUS, "Extended"),
    "triggered": ("\U0001f535", ACCENT, "Triggered"),
    "expired": ("⚪", STATUS_MUTED, "Expired"),
    "rejected": ("\U0001f534", STATUS_CRITICAL, "Rejected"),
}


def status_style(status: str | None) -> tuple[str, str, str]:
    """Return ``(icon, colour, label)`` for a signal status string."""
    key = (status or "unknown").lower()
    return _STATUS_STYLE.get(key, ("⚪", STATUS_MUTED, key.replace("_", " ").title()))


def status_label(status: str | None) -> str:
    """Icon + label, for table cells and captions."""
    icon, _color, label = status_style(status)
    return f"{icon} {label}"


def direction_label(direction: str | None) -> str:
    """Icon + label for a trade direction, using the diverging blue/red pair."""
    d = (direction or "").lower()
    if d == "long":
        return "\U0001f535 LONG"
    if d == "short":
        return "\U0001f534 SHORT"
    return "⚪ FLAT"


def direction_color(direction: str | None) -> str:
    d = (direction or "").lower()
    if d == "long":
        return LONG_COLOR
    if d == "short":
        return SHORT_COLOR
    return NEUTRAL_COLOR


def regime_style(regime: str | None) -> tuple[str, str]:
    """(icon, label) for a market-regime string."""
    mapping = {
        "bull_quiet": ("\U0001f7e2", "Bull -- Quiet"),
        "bull_volatile": ("\U0001f7e2", "Bull -- Volatile"),
        "neutral": ("⚪", "Neutral"),
        "bear_volatile": ("\U0001f534", "Bear -- Volatile"),
        "bear_quiet": ("\U0001f534", "Bear -- Quiet"),
        "unknown": ("❓", "Unknown"),
    }
    return mapping.get((regime or "unknown").lower(), ("❓", (regime or "Unknown").title()))


def badge_color(name: str) -> str:
    """Map a semantic colour name to the ``st.badge``/``st.metric`` colour enum.

    ``st.badge`` only accepts a fixed small vocabulary of named colours, not
    arbitrary hex -- this centralises the mapping from our own semantics to
    that vocabulary so call sites never guess.
    """
    return {
        "good": "green",
        "warning": "orange",
        "serious": "orange",
        "critical": "red",
        "accent": "blue",
        "muted": "gray",
    }.get(name, "gray")


def inject_css() -> None:
    """Global stylesheet: spacing, typography and small structural touches
    that Streamlit's theme file cannot express (card borders, compact
    footer, tightened metric spacing). Call once per page render.
    """
    st.markdown(
        f"""
        <style>
        /* Tighter, more terminal-like vertical rhythm */
        div[data-testid="stVerticalBlock"] > div {{ gap: 0.6rem; }}
        div[data-testid="stMetric"] {{
            background: {SURFACE};
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 8px;
            padding: 0.75rem 1rem 0.6rem 1rem;
        }}
        div[data-testid="stMetricValue"] {{
            font-variant-numeric: tabular-nums;
        }}
        /* Compact persistent disclaimer badge, not a screaming warning block */
        .ct-disclaimer {{
            font-size: 0.72rem;
            color: {INK_MUTED};
            border-top: 1px solid {GRIDLINE};
            margin-top: 1.5rem;
            padding-top: 0.5rem;
            line-height: 1.4;
        }}
        .ct-ribbon {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.4rem 1rem;
            font-size: 0.82rem;
            color: {INK_SECONDARY};
            padding: 0.3rem 0 0.6rem 0;
            border-bottom: 1px solid {GRIDLINE};
            margin-bottom: 0.8rem;
        }}
        .ct-ribbon b {{ color: {INK_PRIMARY}; font-variant-numeric: tabular-nums; }}
        .ct-empty {{
            background: {SURFACE};
            border: 1px dashed {GRIDLINE};
            border-radius: 8px;
            padding: 0.9rem 1.1rem;
            color: {INK_SECONDARY};
            font-size: 0.9rem;
        }}
        .ct-empty code {{
            background: {PAGE_PLANE};
            color: {ACCENT};
            padding: 0.1rem 0.35rem;
            border-radius: 4px;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


__all__ = [
    "ACCENT",
    "ACCENT_STRONG",
    "GRIDLINE",
    "INK_MUTED",
    "INK_PRIMARY",
    "INK_SECONDARY",
    "LONG_COLOR",
    "NEUTRAL_COLOR",
    "PAGE_PLANE",
    "PLOTLY_TEMPLATE",
    "SEQUENTIAL_BLUE",
    "SHORT_COLOR",
    "STATUS_CRITICAL",
    "STATUS_GOOD",
    "STATUS_MUTED",
    "STATUS_SERIOUS",
    "STATUS_WARNING",
    "SURFACE",
    "badge_color",
    "direction_color",
    "direction_label",
    "inject_css",
    "regime_style",
    "status_label",
    "status_style",
]
