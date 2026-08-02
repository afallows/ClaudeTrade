"""Candidate-table shaping: signals/rejections -> display-ready DataFrames.

Kept separate from Streamlit rendering so the shaping logic (which column
goes where, how a status/direction is labelled) is unit-testable without a
Streamlit runtime.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from claudetrade.domain import Signal
from claudetrade.signals.engine import RejectedCandidate
from claudetrade.ui import theme


def empty_state(message: str, command: str | None = None) -> None:
    """Render an empty-state block that always says why, and how to fix it.

    Every empty state in this app must explain the cause and, where one
    exists, name the exact command that would populate it -- never a bare
    "no data" with no next step.
    """
    html = f'<div class="ct-empty">{message}'
    if command:
        html += f'<br><code>{command}</code>'
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)

#: Column order for the candidate table (also hides internal id columns from view).
CANDIDATE_COLUMN_ORDER = [
    "Symbol",
    "Direction",
    "Strategy",
    "Score",
    "Confidence %",
    "Status",
    "Entry Low",
    "Entry High",
    "Stop",
    "Target 1",
    "R:R",
    "Days to Earnings",
    "Session",
]


def signals_dataframe(
    signals: list[Signal], status_by_id: dict[str, str] | None = None
) -> pd.DataFrame:
    """Flatten signals into a display-ready DataFrame.

    One row per signal. ``Signal ID`` is retained (not in
    ``CANDIDATE_COLUMN_ORDER``) so a caller can map a selected row back to its
    originating signal without a second lookup.
    """
    status_by_id = status_by_id or {}
    rows = []
    for sig in signals:
        rows.append(
            {
                "Symbol": sig.symbol,
                "Direction": theme.direction_label(str(sig.direction)),
                "Strategy": sig.strategy,
                "Score": round(sig.overall_score, 1),
                "Confidence %": round(sig.confidence * 100.0, 1),
                "Status": theme.status_label(status_by_id.get(sig.signal_id)),
                "Entry Low": sig.plan.entry_low,
                "Entry High": sig.plan.entry_high,
                "Stop": sig.plan.stop_loss,
                "Target 1": sig.plan.targets[0] if sig.plan.targets else None,
                "R:R": round(sig.plan.reward_risk_ratio, 2),
                "Days to Earnings": sig.days_to_earnings,
                "Session": sig.session,
                "Signal ID": sig.signal_id,
            }
        )
    columns = [*CANDIDATE_COLUMN_ORDER, "Signal ID"]
    return pd.DataFrame(rows, columns=columns)


def signals_column_config() -> dict[str, object]:
    """``st.dataframe`` column_config for ``signals_dataframe``'s output.

    Score renders as a progress bar (0-100); confidence as a percentage bar;
    prices as formatted currency; session as a plain date.
    """
    return {
        "Score": st.column_config.ProgressColumn(
            "Score", help="Blended overall signal score (0-100).", min_value=0, max_value=100, format="%.0f"
        ),
        "Confidence %": st.column_config.ProgressColumn(
            "Confidence", help="Model confidence in this signal.", min_value=0, max_value=100, format="%.0f%%"
        ),
        "Entry Low": st.column_config.NumberColumn("Entry Low", format="$%.2f"),
        "Entry High": st.column_config.NumberColumn("Entry High", format="$%.2f"),
        "Stop": st.column_config.NumberColumn("Stop", format="$%.2f"),
        "Target 1": st.column_config.NumberColumn("Target 1", format="$%.2f"),
        "R:R": st.column_config.NumberColumn("R:R", format="%.2f:1"),
        "Days to Earnings": st.column_config.NumberColumn("Days to Earnings"),
        "Session": st.column_config.DateColumn("Session"),
    }


def rejected_dataframe(rejected: list[RejectedCandidate]) -> pd.DataFrame:
    """Near-miss candidates and why they did not become a signal.

    One row per (symbol, strategy, stage) rejection, most informative first
    (score/limits/sizing rejections -- the "so close" cases -- ahead of
    structural gate failures).
    """
    stage_priority = {"score": 0, "sizing": 1, "limits": 2, "gates": 3, "strategy": 4}
    rows = [
        {
            "Symbol": r.symbol,
            "Strategy": r.strategy,
            "Stage": r.stage,
            "Reasons": "; ".join(r.reasons) if r.reasons else "-",
            "_priority": stage_priority.get(r.stage, 9),
        }
        for r in rejected
    ]
    df = pd.DataFrame(rows, columns=["Symbol", "Strategy", "Stage", "Reasons", "_priority"])
    if df.empty:
        return df.drop(columns=["_priority"])
    df = df.sort_values(["_priority", "Symbol"]).drop(columns=["_priority"]).reset_index(drop=True)
    return df


def apply_candidate_filters(
    signals: list[Signal],
    *,
    directions: set[str] | None = None,
    min_score: float = 0.0,
    min_confidence: float = 0.0,
    strategies: set[str] | None = None,
    max_days_to_earnings: int | None = None,
) -> list[Signal]:
    """Pure filter logic behind the Scanner's filter widgets.

    Kept separate from the widgets themselves so the filtering rules are
    unit-testable without a Streamlit runtime.
    """
    out = []
    for sig in signals:
        if sig.overall_score < min_score:
            continue
        if sig.confidence < min_confidence:
            continue
        if directions is not None and str(sig.direction) not in directions:
            continue
        if strategies is not None and sig.strategy not in strategies:
            continue
        if max_days_to_earnings is not None and (
            sig.days_to_earnings is None or sig.days_to_earnings > max_days_to_earnings
        ):
            continue
        out.append(sig)
    return out


__all__ = [
    "CANDIDATE_COLUMN_ORDER",
    "apply_candidate_filters",
    "rejected_dataframe",
    "signals_column_config",
    "signals_dataframe",
]
