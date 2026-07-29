"""Formatting utilities for display and export.

Handles timezone conversion, number formatting, date formatting, and
sanitization for export.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from claudetrade.config import AppConfig
from claudetrade.domain import Direction, TradeOutcome


def format_percent(value: float, decimals: int = 2) -> str:
    """Format a value as a percentage string."""
    if value is None or not isinstance(value, (int, float)):
        return "-"
    return f"{value:.{decimals}f}%"


def format_currency(value: float, decimals: int = 2) -> str:
    """Format a value as USD currency."""
    if value is None or not isinstance(value, (int, float)):
        return "-"
    return f"${value:,.{decimals}f}"


def format_price(value: float, decimals: int = 4) -> str:
    """Format a stock price."""
    if value is None or not isinstance(value, (int, float)):
        return "-"
    if value >= 1:
        return f"${value:.{decimals}f}"
    return f"${value:.{decimals}f}"


def format_ratio(value: float, decimals: int = 2) -> str:
    """Format a ratio (win:loss, etc.)."""
    if value is None or not isinstance(value, (int, float)):
        return "-"
    return f"{value:.{decimals}f}:1"


def format_integer(value: int | None) -> str:
    """Format an integer with thousands separator."""
    if value is None:
        return "-"
    return f"{value:,d}"


def format_datetime(dt_val: dt.datetime | None, config: AppConfig) -> str:
    """Format a UTC datetime in the operator's configured timezone.

    Note: Timezone conversion requires pytz when using named zones.
    Falls back to ISO format if timezone is unavailable.
    """
    if dt_val is None:
        return "-"
    if dt_val.tzinfo is None:
        # Assume UTC if no timezone info
        dt_val = dt_val.replace(tzinfo=dt.UTC)
    try:
        import pytz
        tz = pytz.timezone(config.ui.display_timezone)
        local_dt = dt_val.astimezone(tz)
        return local_dt.strftime("%Y-%m-%d %H:%M %Z")
    except (ImportError, Exception):
        # Fallback: show UTC time if pytz unavailable
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=dt.UTC)
        return dt_val.strftime("%Y-%m-%d %H:%M UTC")


def format_date(d: dt.date | None) -> str:
    """Format a date."""
    if d is None:
        return "-"
    return d.strftime("%Y-%m-%d")


def format_direction(direction: Direction) -> str:
    """Format a trade direction for display."""
    if direction == Direction.LONG:
        return "📈 LONG"
    elif direction == Direction.SHORT:
        return "📉 SHORT"
    return "- FLAT"


def format_outcome(outcome: TradeOutcome | None) -> str:
    """Format a trade outcome with emoji."""
    if outcome is None:
        return "-"
    if outcome == TradeOutcome.WIN:
        return "✅ WIN"
    elif outcome == TradeOutcome.LOSS:
        return "❌ LOSS"
    elif outcome == TradeOutcome.BREAKEVEN:
        return "⊘ BREAKEVEN"
    return "-"


def format_status(status_str: str) -> str:
    """Format a signal status with color-coded emoji."""
    status_map = {
        "actionable": "🎯 Actionable",
        "approaching": "🔜 Approaching",
        "extended": "🚫 Extended",
        "triggered": "✅ Triggered",
        "expired": "⏰ Expired",
        "rejected": "❌ Rejected",
    }
    return status_map.get(status_str, status_str)


def format_market_regime(regime: str) -> str:
    """Format a market regime with icon."""
    regime_map = {
        "bull_quiet": "🐂 Bull Quiet",
        "bull_volatile": "🐂📈 Bull Volatile",
        "neutral": "➡️ Neutral",
        "bear_volatile": "🐻📉 Bear Volatile",
        "bear_quiet": "🐻 Bear Quiet",
        "unknown": "❓ Unknown",
    }
    return regime_map.get(regime, regime)


def format_large_number(value: float | int | None, decimals: int = 1) -> str:
    """Format large numbers with K/M/B suffix."""
    if value is None:
        return "-"
    value = float(value)
    if abs(value) >= 1e9:
        return f"{value / 1e9:.{decimals}f}B"
    elif abs(value) >= 1e6:
        return f"{value / 1e6:.{decimals}f}M"
    elif abs(value) >= 1e3:
        return f"{value / 1e3:.{decimals}f}K"
    return f"{value:.{decimals}f}"


def format_confidence(value: float) -> str:
    """Format confidence as a percentage with bar."""
    if value is None or not (0 <= value <= 1):
        return "-"
    pct = value * 100
    if pct >= 80:
        emoji = "🟢"
    elif pct >= 60:
        emoji = "🟡"
    else:
        emoji = "🔴"
    return f"{emoji} {pct:.0f}%"


def show_disclaimer() -> None:
    """Display the research-signals disclaimer on every page."""
    from claudetrade.version import DISCLAIMER

    st.warning(f"⚠️ {DISCLAIMER}")
