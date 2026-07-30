"""Shared page chrome: header, persistent footer, and the sidebar status ribbon.

The sidebar status block is pulled from the same pipeline status APIs the CLI
uses (``pipeline.provider_status()``, ``pipeline.ledger``, ``PaperPortfolio``)
-- never a mock value -- so "data freshness" / "provider status" / "regime" /
"kill switch" reported here are exactly what an operator would see running
the equivalent CLI commands.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import streamlit as st

from claudetrade.config import AppConfig
from claudetrade.domain import Signal
from claudetrade.pipeline import Pipeline
from claudetrade.providers.base import ProviderStatus
from claudetrade.ui import theme
from claudetrade.ui.data_access import DataFreshness, data_freshness
from claudetrade.ui.formatting import format_datetime
from claudetrade.version import CODE_VERSION, DISCLAIMER


@dataclass(slots=True)
class ProviderSummary:
    """How many configured providers are reachable right now."""

    available: int
    total: int
    degraded: list[str]

    @property
    def all_ok(self) -> bool:
        return self.total > 0 and self.available == self.total


def summarize_providers(statuses: list[ProviderStatus]) -> ProviderSummary:
    """Reduce a provider-status report to a pass/fail count plus the failing names."""
    degraded = [s.name for s in statuses if not s.available]
    return ProviderSummary(available=len(statuses) - len(degraded), total=len(statuses), degraded=degraded)


def latest_regime(signals: list[Signal]) -> tuple[str, dt.date | None]:
    """Regime value and session from the most recently created signal.

    Returns ``("unknown", None)`` when no signal has ever been recorded --
    the regime shown must come from a real scan, never an invented default.
    """
    if not signals:
        return "unknown", None
    latest = max(signals, key=lambda s: s.created_at)
    return str(latest.regime), latest.session


def page_header(icon: str, title: str, subtitle: str = "") -> None:
    """Title plus a compact, persistent research-only badge (never a full warning block)."""
    header_col, badge_col = st.columns([5, 2])
    with header_col:
        st.title(f"{icon} {title}")
        if subtitle:
            st.caption(subtitle)
    with badge_col:
        st.write("")
        st.badge("RESEARCH SIGNALS ONLY", icon="⚠️", color="orange")


def render_footer() -> None:
    """The full disclaimer, once, as a small persistent footer -- not per screen."""
    st.markdown(f'<div class="ct-disclaimer">{DISCLAIMER}</div>', unsafe_allow_html=True)


def render_sidebar_status(config: AppConfig, pipeline: Pipeline) -> None:
    """Data freshness, provider status, regime badge, and kill-switch state."""
    st.sidebar.divider()
    st.sidebar.caption("STATUS")

    _render_freshness(config, pipeline)
    _render_providers(pipeline)
    _render_regime(pipeline)
    _render_kill_switch(config, pipeline)

    st.sidebar.caption(f"v{CODE_VERSION}")


def _render_freshness(config: AppConfig, pipeline: Pipeline) -> None:
    try:
        freshness: DataFreshness = data_freshness(pipeline.db)
    except Exception as exc:  # pragma: no cover - defensive, mirrors screen pattern
        st.sidebar.error(f"data status unavailable: {exc}")
        return
    if not freshness.has_data:
        st.sidebar.warning("No market data yet -- run `claudetrade refresh`")
        return
    st.sidebar.metric("Latest session", freshness.latest_session.isoformat())
    st.sidebar.caption(
        f"{freshness.symbol_count} symbols with stored bars -- last refresh "
        f"{format_datetime(freshness.latest_ingested_at, config)}"
    )


def _render_providers(pipeline: Pipeline) -> None:
    try:
        statuses = pipeline.provider_status()
    except Exception as exc:
        st.sidebar.error(f"provider status unavailable: {exc}")
        return
    summary = summarize_providers(statuses)
    if not summary.total:
        st.sidebar.info("No providers configured")
        return
    if summary.all_ok:
        st.sidebar.success(f"Providers: {summary.available}/{summary.total} OK")
    else:
        st.sidebar.warning(
            f"Providers: {summary.available}/{summary.total} OK "
            f"({', '.join(summary.degraded)} degraded)"
        )
    with st.sidebar.expander("Provider detail"):
        for status in statuses:
            icon = "🟢" if status.available else "🔴"
            st.write(f"{icon} **{status.name}** ({status.kind}) -- {status.message}")


def _render_regime(pipeline: Pipeline) -> None:
    try:
        recent_signals = pipeline.ledger.recent(limit=50)
    except Exception as exc:
        st.sidebar.error(f"regime unavailable: {exc}")
        return
    regime, session = latest_regime(recent_signals)
    icon, label = theme.regime_style(regime)
    if session is not None:
        st.sidebar.write(f"{icon} **Regime**: {label}")
        st.sidebar.caption(f"as of the {session.isoformat()} scan")
    else:
        st.sidebar.write(f"{icon} **Regime**: unknown -- no scan run yet")


def _render_kill_switch(config: AppConfig, pipeline: Pipeline) -> None:
    try:
        from claudetrade.paper.portfolio import PaperPortfolio

        account = PaperPortfolio(config, pipeline.db).account()
        engaged = bool(account.kill_switch_engaged or config.trading.kill_switch_engaged)
    except Exception as exc:
        st.sidebar.error(f"account status unavailable: {exc}")
        return
    if engaged:
        st.sidebar.error("🔴 KILL SWITCH ENGAGED -- no new entries")
    else:
        st.sidebar.success("🟢 Kill switch off")


__all__ = [
    "ProviderSummary",
    "latest_regime",
    "page_header",
    "render_footer",
    "render_sidebar_status",
    "summarize_providers",
]
