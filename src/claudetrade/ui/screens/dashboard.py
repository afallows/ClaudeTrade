"""Dashboard: market regime, top candidates, paper account, performance, status.

Every section reads through the same pipeline/ledger/paper-portfolio APIs the
CLI uses -- nothing here is sample or placeholder data. Every empty state
names the exact command that would populate it.
"""

from __future__ import annotations

import streamlit as st

from claudetrade.ui import theme
from claudetrade.ui.charts import create_sparkline
from claudetrade.ui.components.layout import page_header
from claudetrade.ui.components.stats import metric_tile, significance_caveat
from claudetrade.ui.components.tables import (
    empty_state,
    signals_column_config,
    signals_dataframe,
)
from claudetrade.ui.data_access import data_freshness
from claudetrade.ui.formatting import format_currency, format_datetime
from claudetrade.ui.state import get_config, get_pipeline

#: How many candidates per side the "top candidates" tables show.
TOP_N = 5


def top_candidates(signals, direction: str, n: int = TOP_N):
    """The best ``n`` signals for one direction, ranked by overall score.

    Pure helper (no Streamlit/DB calls) so the ranking logic is unit-testable.
    """
    side = [s for s in signals if str(s.direction) == direction]
    return sorted(side, key=lambda s: s.overall_score, reverse=True)[:n]


def page_dashboard() -> None:
    """Render the dashboard."""
    config = get_config()
    pipeline = get_pipeline(config)
    theme.inject_css()
    page_header("📊", "Dashboard", "Market state, candidates, and paper-account performance.")

    _render_status_ribbon(config, pipeline)
    _render_regime_and_candidates(pipeline)
    _render_paper_account(config, pipeline)
    _render_performance_tiles(config, pipeline)
    _render_provider_status(pipeline)


def _render_status_ribbon(config, pipeline) -> None:
    freshness = data_freshness(pipeline.db)
    recent = pipeline.ledger.recent(limit=1)
    last_scan = format_datetime(recent[0].created_at, config) if recent else "never"
    last_refresh = (
        format_datetime(freshness.latest_ingested_at, config) if freshness.has_data else "never"
    )
    st.markdown(
        f'<div class="ct-ribbon">'
        f"<span>Last refresh: <b>{last_refresh}</b></span>"
        f"<span>Last scan: <b>{last_scan}</b></span>"
        f"<span>Symbols with data: <b>{freshness.symbol_count}</b></span>"
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_regime_and_candidates(pipeline) -> None:
    st.subheader("Market Regime &amp; Top Candidates")

    try:
        recent = pipeline.ledger.recent(limit=200)
    except Exception as exc:
        st.error(f"Could not load signals: {exc}")
        return

    regime_col, longs_col, shorts_col = st.columns([1, 2, 2])

    with regime_col, st.container(border=True):
        if recent:
            latest = max(recent, key=lambda s: s.created_at)
            _icon, label = theme.regime_style(str(latest.regime))
            st.metric("Regime", label)
            st.caption(f"as of the {latest.session.isoformat()} scan")
        else:
            st.metric("Regime", "Unknown")
            st.caption("No scan has run yet")

    def _candidate_table(container, direction: str, label: str) -> None:
        with container:
            st.write(f"**Top {TOP_N} {label}**")
            picks = top_candidates(recent, direction)
            if not picks:
                if not recent:
                    empty_state(
                        "No signals for this session.",
                        "claudetrade scan",
                    )
                else:
                    empty_state(f"No {label.lower()} candidates in the current signal set.")
                return
            status_by_id = {
                s.signal_id: (pipeline.ledger.current_status(s.signal_id) or None)
                for s in picks
            }
            status_by_id = {k: (v.value if v else None) for k, v in status_by_id.items()}
            df = signals_dataframe(picks, status_by_id)
            st.dataframe(
                df,
                column_order=[c for c in df.columns if c != "Signal ID"],
                column_config=signals_column_config(),
                hide_index=True,
                width="stretch",
            )

    _candidate_table(longs_col, "long", "Long")
    _candidate_table(shorts_col, "short", "Short")


def _render_paper_account(config, pipeline) -> None:
    st.subheader("Paper Account")
    try:
        from claudetrade.paper.portfolio import PaperPortfolio

        portfolio = PaperPortfolio(config, pipeline.db)
        account = portfolio.account()
    except Exception as exc:
        st.error(f"Could not load the paper account: {exc}")
        return

    tile_col, spark_col = st.columns([2, 3])
    with tile_col:
        m1, m2, m3 = st.columns(3)
        m1.metric("Equity", format_currency(account.equity))
        m2.metric("Cash", format_currency(account.cash))
        m3.metric("Realised P&amp;L", format_currency(account.realised_pnl))
        if account.kill_switch_engaged or config.trading.kill_switch_engaged:
            st.error("🔴 Kill switch engaged -- no new entries will be accepted")

    curve = portfolio.equity_curve()
    with spark_col:
        if curve:
            st.caption("Equity, recorded history")
            fig = create_sparkline([p.session for p in curve], [p.equity for p in curve])
            st.plotly_chart(fig, theme=None, config={"displayModeBar": False})
        else:
            empty_state(
                "No recorded equity history yet -- the curve fills in once a paper "
                "position has been opened and marked at least once.",
                "claudetrade paper open <signal-id>",
            )

    marks = portfolio.positions({s: b.close for s, b in pipeline_latest_bars(pipeline).items()})
    if marks:
        rows = [
            {
                "Symbol": p.symbol,
                "Direction": theme.direction_label(p.direction.value),
                "Shares": p.shares,
                "Entry": p.entry_price,
                "Last": p.last_price,
                "Unrealised P&L": p.unrealised_pnl,
                "Unrealised %": p.unrealised_pct,
                "Days Held": p.days_held,
                "Needs Attention": "; ".join(p.needs_attention()) or "-",
            }
            for p in marks
        ]
        st.dataframe(
            rows,
            hide_index=True,
            width="stretch",
            column_config={
                "Entry": st.column_config.NumberColumn(format="$%.2f"),
                "Last": st.column_config.NumberColumn(format="$%.2f"),
                "Unrealised P&L": st.column_config.NumberColumn(format="$%.2f"),
                "Unrealised %": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )
    else:
        empty_state(
            "No open paper positions.",
            "claudetrade paper open <signal-id>",
        )

    closed = portfolio.closed_trades(limit=10)
    st.write("**Recent wins / losses**")
    if closed:
        rows = [
            {
                "Symbol": t.symbol,
                "Direction": theme.direction_label(t.direction),
                "Exit": t.exit_session,
                "Outcome": (t.outcome or "-").upper(),
                "Net P&L": t.net_pnl,
                "R-Multiple": round(t.r_multiple, 2),
                "Reason": t.exit_reason or "-",
            }
            for t in closed
        ]
        st.dataframe(
            rows,
            hide_index=True,
            width="stretch",
            column_config={"Net P&L": st.column_config.NumberColumn(format="$%.2f")},
        )
    else:
        empty_state(
            "No closed paper trades yet -- outcomes appear here once a position "
            "hits its stop, target, or time stop.",
            "claudetrade paper process",
        )


def pipeline_latest_bars(pipeline) -> dict[str, object]:
    """Latest close per open paper symbol, via the same helper the broker uses."""
    from claudetrade.paper.broker import PaperBroker

    return PaperBroker(pipeline.config, pipeline.db).latest_bars_for_open_positions()


def _render_performance_tiles(config, pipeline) -> None:
    st.subheader("Performance Snapshot (paper account)")
    from claudetrade.paper.portfolio import PaperPortfolio

    portfolio = PaperPortfolio(config, pipeline.db)
    perf = portfolio.performance()
    closed_count = perf["closed_trades"]

    floor = config.backtest.min_trades_for_validation
    is_significant = closed_count >= floor
    reason = None
    if not is_significant:
        reason = f"only {closed_count} completed paper trade(s), below the {floor}-trade minimum"

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        wl = perf["win_loss_ratio"]
        metric_tile(
            "Win/Loss Ratio",
            "inf" if wl == float("inf") else f"{wl:.2f}",
            help_text="Wins / losses on closed paper trades.",
        )
    with c2:
        metric_tile("Expectancy", format_currency(perf["expectancy"]), help_text="Average net P&L per closed trade.")
    with c3:
        curve = portfolio.equity_curve()
        max_dd = max((p.drawdown_pct for p in curve), default=None)
        metric_tile(
            "Max Drawdown",
            f"{max_dd:.2f}%" if max_dd is not None else None,
            unavailable_reason=None if curve else "No equity history recorded yet.",
        )
    with c4:
        metric_tile("Closed Trades", str(closed_count))

    significance_caveat(is_significant=is_significant, reason=reason, trade_count=closed_count)
    for warning in perf.get("warnings", []):
        st.caption(f"⚠️ {warning}")


def _render_provider_status(pipeline) -> None:
    st.subheader("Data Provider Status")
    try:
        statuses = pipeline.provider_status()
    except Exception as exc:
        st.error(f"Error loading provider status: {exc}")
        return
    if not statuses:
        empty_state("No provider status available.")
        return
    rows = [
        {
            "Provider": s.name,
            "Kind": s.kind,
            "Available": "yes" if s.available else "no",
            "Configured": "yes" if s.configured else "no",
            "Point-in-time": "yes" if s.supports_point_in_time else "no",
            "Message": s.message,
        }
        for s in statuses
    ]
    st.dataframe(rows, hide_index=True, width="stretch")
