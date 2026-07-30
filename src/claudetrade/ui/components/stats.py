"""Stat-tile rendering with honest 'unavailable' states.

A ratio metric that came out ``None`` (see
``claudetrade.backtest.metrics.PerformanceMetrics.unavailable_reasons``) is
rendered as "unavailable" with its reason surfaced -- never silently replaced
by 0.0, "-", or an inf/NaN standing in for "unmeasurable".
"""

from __future__ import annotations

import streamlit as st


def metric_tile(
    label: str,
    value: str | None,
    *,
    help_text: str | None = None,
    unavailable_reason: str | None = None,
) -> None:
    """Render one ``st.metric``, substituting an explained 'unavailable' when needed."""
    if unavailable_reason is not None or value is None:
        st.metric(label, "unavailable", help=unavailable_reason or "Not enough data yet.")
    else:
        st.metric(label, value, help=help_text)


def significance_caveat(*, is_significant: bool, reason: str | None, trade_count: int) -> None:
    """The below-floor caveat banner ADR-0007 requires whenever a sample isn't validated."""
    if is_significant:
        return
    st.warning(
        f"NOT STATISTICALLY SIGNIFICANT ({trade_count} completed trade(s)): {reason or 'sample too small'}. "
        "Every ratio and point estimate above is directional only, not evidence of a durable edge."
    )


__all__ = ["metric_tile", "significance_caveat"]
