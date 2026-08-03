"""Ticker Detail: the centrepiece screen -- full technical + sentiment picture.

Candlestick with volume, entry/stop/target levels for the active signal,
earnings-date markers, an indicator-driven RSI panel, and a sentiment +
mention-volume timeline built from ``symbol_sentiment_daily`` -- all on one
shared time axis.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from claudetrade.domain import SignalStatus
from claudetrade.ui import theme
from claudetrade.ui.charts import create_ticker_chart
from claudetrade.ui.components.layout import page_header
from claudetrade.ui.components.tables import empty_state
from claudetrade.ui.data_access import (
    analyst_sentiment,
    earnings_dates,
    institutional_sentiment,
    known_symbols,
    price_bars,
    research_overlay,
    sentiment_timeline,
)
from claudetrade.ui.formatting import (
    format_confidence,
    format_date,
    format_large_number,
    format_price,
    format_status,
)
from claudetrade.ui.state import get_config, get_pipeline


def active_signal(signals):
    """The signal to draw entry/stop/target levels for: the newest tradable one.

    Falls back to the newest signal overall if none is currently tradable, so
    the chart still shows the most recent thesis's levels even after it has
    triggered or expired. Pure helper -- unit-testable without Streamlit.
    """
    if not signals:
        return None
    tradable = [s for s in signals if s.is_tradable]
    pool = tradable or signals
    return max(pool, key=lambda s: s.created_at)


def page_ticker_detail() -> None:
    """Render the ticker detail page."""
    config = get_config()
    pipeline = get_pipeline(config)
    theme.inject_css()
    page_header("📈", "Ticker Detail", "Full technical, signal, and sentiment picture for one symbol.")

    try:
        symbols = known_symbols(pipeline.db)
    except Exception as exc:
        st.error(f"Could not load the symbol list: {exc}")
        return

    if not symbols:
        empty_state(
            "No symbols with stored price history yet.",
            "claudetrade refresh",
        )
        return

    default_symbol = _default_symbol(pipeline, symbols)
    symbol = st.selectbox(
        "Symbol",
        options=symbols,
        index=symbols.index(default_symbol) if default_symbol in symbols else 0,
        key="ticker_symbol_select",
    )

    try:
        signals = pipeline.ledger.for_symbol(symbol, limit=50)
    except Exception as exc:
        st.error(f"Error loading signals for {symbol}: {exc}")
        signals = []

    _render_current_signal(config, pipeline, symbol, signals)
    _render_chart(config, pipeline, symbol, signals)
    _render_analyst_sentiment(pipeline, symbol)
    _render_institutional_sentiment(pipeline, symbol)
    _render_signal_history(pipeline, signals)


def _default_symbol(pipeline, symbols: list[str]) -> str | None:
    try:
        recent = pipeline.ledger.recent(limit=50)
    except Exception:
        return symbols[0] if symbols else None
    if not recent:
        return symbols[0] if symbols else None
    best = max(recent, key=lambda s: s.overall_score)
    return best.symbol


def _render_current_signal(config, pipeline, symbol: str, signals) -> None:
    st.subheader(f"Current Signal: {symbol}")
    sig = active_signal(signals)
    if sig is None:
        empty_state(
            f"No signals recorded for {symbol} yet.",
            "claudetrade scan",
        )
        return

    status = pipeline.ledger.current_status(sig.signal_id)
    # Single-signal lookup: still ONE batched ResearchLedger query (a list of
    # one), matching the same helper the Scanner grid uses -- never a
    # separate ad hoc query path for this screen.
    overlay = research_overlay(pipeline.db, [sig], config)[sig.signal_id]

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Score", f"{overlay.effective_score:.0f}")
        if overlay.has_research:
            st.caption(f"engine: {sig.overall_score:.0f}")
        st.metric("Confidence", format_confidence(sig.confidence))
    with col2:
        st.metric("Status", format_status(status.value if status else "unknown"))
        st.metric("Direction", theme.direction_label(str(sig.direction)))
    with col3:
        st.metric("Entry Zone", f"{format_price(sig.plan.entry_low)} - {format_price(sig.plan.entry_high)}")
        st.metric("Stop Loss", format_price(sig.plan.stop_loss))

    with st.expander("Thesis, invalidation and component scores", expanded=False):
        if overlay.has_research and overlay.latest is not None:
            _render_research_revision(overlay.latest)
            st.divider()

        st.write(f"**Thesis** (engine): {sig.thesis or 'No thesis available'}")
        st.write(
            "**Invalidation** (engine): "
            + (", ".join(sig.invalidation) if sig.invalidation else "none recorded")
        )
        cols = st.columns(4)
        for i, (name, score) in enumerate(sig.components.as_dict().items()):
            cols[i % 4].metric(name.replace("_", " ").title(), f"{score:.0f}")


def _render_research_revision(latest: dict) -> None:
    """The latest accepted research revision, ahead of the engine's own text.

    ``latest`` is the dict shape ``ResearchLedger.latest_research_revisions``
    returns (via ``data_access.research_overlay``): ``thesis``/
    ``invalidation`` of ``None`` means the engine's own text is unchanged by
    this revision, not that the fields are blank.
    """
    created = latest["created_at"]
    created_label = created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else str(created)
    st.markdown(f"**Research revision (r{latest['revision']}, {latest['actor']}, {created_label})**")
    st.write(f"**Revised thesis**: {latest['thesis'] or 'unchanged from the engine text'}")
    revised_invalidation = latest["invalidation"]
    st.write(
        "**Revised invalidation**: "
        + (", ".join(revised_invalidation) if revised_invalidation else "unchanged from the engine text")
    )
    adjustments = latest["score_adjustments"]
    if adjustments:
        st.write(
            "**Score adjustments**: "
            + ", ".join(f"{name}: {delta:+.1f}" for name, delta in adjustments.items())
        )
    st.write(f"**Rationale**: {latest['rationale']}")
    if latest["sources"]:
        st.write("**Sources**: " + ", ".join(latest["sources"]))


def _render_chart(config, pipeline, symbol: str, signals) -> None:
    st.subheader("Price Action")

    ctl1, ctl2, ctl3, ctl4 = st.columns(4)
    with ctl1:
        lookback_days = st.slider(
            "Lookback (days)", min_value=60, max_value=1000,
            value=int(config.ui.chart_lookback_days), step=30,
        )
    with ctl2:
        sma_choices = st.multiselect(
            "Moving averages", options=[20, 50, 200], default=[20, 50],
            format_func=lambda w: f"SMA {w}",
        )
    with ctl3:
        show_bollinger = st.checkbox("Bollinger Bands (20, 2 stdev)", value=False)
    with ctl4:
        show_rsi = st.checkbox("RSI panel", value=True)

    end = dt.datetime.now(dt.UTC).date()
    start = end - dt.timedelta(days=lookback_days)
    try:
        bars = price_bars(pipeline.db, symbol, start=start, end=end)
    except Exception as exc:
        st.error(f"Could not load price history: {exc}")
        return

    if not bars:
        empty_state(
            f"No price history stored for {symbol} in the last {lookback_days} days.",
            "claudetrade refresh",
        )
        return

    sig = active_signal(signals)
    entry_low = entry_high = stop_loss = None
    targets: list[float] = []
    if sig is not None and sig.status not in (SignalStatus.EXPIRED, SignalStatus.REJECTED):
        entry_low, entry_high = sig.plan.entry_low, sig.plan.entry_high
        stop_loss = sig.plan.stop_loss
        targets = list(sig.plan.targets)

    try:
        report_dates = earnings_dates(pipeline.db, symbol)
    except Exception:
        report_dates = []

    try:
        sentiment = sentiment_timeline(pipeline.db, symbol)
        sentiment = [p for p in sentiment if start <= p.session <= end]
    except Exception:
        sentiment = []

    fig = create_ticker_chart(
        bars,
        title=f"{symbol} -- {len(bars)} sessions",
        sma_windows=tuple(sorted(sma_choices)),
        show_bollinger=show_bollinger,
        show_rsi=show_rsi,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        targets=targets,
        earnings_dates=report_dates,
        sentiment=sentiment,
    )
    st.plotly_chart(fig, theme=None, config={"displayModeBar": True, "displaylogo": False})

    if not sentiment:
        st.caption(
            "No sentiment/mention data for this symbol in the selected window -- run "
            "`claudetrade refresh` with social sources enabled to populate it."
        )
    upcoming = [d for d in report_dates if d >= end]
    if upcoming:
        st.caption(f"Next earnings: {format_date(upcoming[0])} ({(upcoming[0] - end).days} days out)")
    else:
        st.caption("No upcoming earnings date on file for this symbol.")


def _render_analyst_sentiment(pipeline, symbol: str) -> None:
    """TipRanks-sourced analyst-consensus block: consensus + B/H/S counts,
    price-target mean vs. the most recent stored close, recent rating
    actions, and the last earnings surprise. Never makes a network call --
    reads only what the last ``claudetrade refresh`` already stored (see
    ``ui.data_access.analyst_sentiment``).
    """
    st.subheader("Analyst Sentiment")
    try:
        overlay = analyst_sentiment(pipeline.db, symbol)
    except Exception as exc:
        st.error(f"Could not load analyst sentiment: {exc}")
        return

    if not overlay.available or overlay.snapshot is None:
        empty_state(
            f"No stored analyst-sentiment snapshot for {symbol} -- either this "
            "installation has not refreshed since this feature was added, or "
            "TipRanks has no analyst coverage for this symbol at all.",
            "claudetrade refresh",
        )
        return

    snap = overlay.snapshot
    delta = overlay.delta

    try:
        recent_bars = price_bars(pipeline.db, symbol)
        current_price = recent_bars[-1].close if recent_bars else None
    except Exception:
        current_price = None

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric(
            "Consensus Rating",
            snap.consensus_rating if snap.consensus_rating is not None else "n/a",
        )
        st.caption("TipRanks' own 1-5 scale (direction unconfirmed)")
    with col2:
        st.metric("Buy / Hold / Sell", f"{snap.buy_count} / {snap.hold_count} / {snap.sell_count}")
        if delta is not None and delta.has_previous:
            st.caption(
                f"vs prior: {delta.buy_count_change:+d} / {delta.hold_count_change:+d} / "
                f"{delta.sell_count_change:+d}"
            )
    with col3:
        pt_mean = format_price(snap.price_target_mean) if snap.price_target_mean is not None else "n/a"
        st.metric("Price Target (mean)", f"{pt_mean} {snap.price_target_currency or ''}".strip())
        if current_price is not None and snap.price_target_mean is not None and current_price:
            upside = (snap.price_target_mean - current_price) / current_price * 100.0
            st.caption(f"{upside:+.1f}% vs last close ({format_price(current_price)})")
    with col4:
        st.metric("Analyst Count", snap.analyst_count)
        if delta is not None and delta.has_previous and delta.coverage_change is not None:
            st.caption(f"coverage change: {delta.coverage_change:+d}")

    surprise_col, next_col = st.columns(2)
    with surprise_col:
        surprise = (
            f"{snap.last_eps_surprise_pct:+.1f}%" if snap.last_eps_surprise_pct is not None else "n/a"
        )
        st.caption(f"Last EPS surprise: {surprise}")
    with next_col:
        next_eps = (
            f"{snap.next_earnings_estimate_eps:.2f}"
            if snap.next_earnings_estimate_eps is not None
            else "n/a"
        )
        st.caption(f"Next earnings EPS estimate: {next_eps}")

    with st.expander("Recent analyst rating actions", expanded=False):
        if not snap.recent_rating_actions:
            st.caption("No individual rating actions stored for this symbol.")
        else:
            rows = [
                {
                    "Date": a.date,
                    "Firm": a.firm,
                    "Analyst": a.analyst_name,
                    "Rating": (a.rating_label or "unknown").title(),
                    "Action": (a.action_label or f"id {a.action_id}") if a.action_id else "n/a",
                    "Price Target": a.price_target,
                    "Prior Target": a.old_price_target,
                    "Stars": a.analyst_stars,
                }
                for a in snap.recent_rating_actions
            ]
            st.dataframe(
                rows,
                hide_index=True,
                width="stretch",
                column_config={
                    "Date": st.column_config.DateColumn(),
                    "Price Target": st.column_config.NumberColumn(format="$%.2f"),
                    "Prior Target": st.column_config.NumberColumn(format="$%.2f"),
                    "Stars": st.column_config.NumberColumn(format="%.1f"),
                },
            )
        st.caption(
            "actionId semantics beyond 'upgrade'/'reiterate' are not documented by "
            "TipRanks; an unrecognised action shows its raw id rather than a guessed "
            "label -- see docs/api-providers.md."
        )


def _render_institutional_sentiment(pipeline, symbol: str) -> None:
    """TipRanks-sourced insider/hedge-fund ("institutional") sentiment
    block: blended score with per-axis breakdown and staleness, recent
    insider transactions (role flags + SEC links), and notable hedge-fund
    holder moves. Never makes a network call -- reads only what the last
    ``claudetrade refresh`` already stored (see
    ``ui.data_access.institutional_sentiment``).

    **Research overlay only** -- this score is not fed into
    ``signals.scoring.ComponentScores`` or any scan/backtest strategy (see
    ``providers.market.tipranks_institutional.institutional_score``'s own
    docstring).
    """
    st.subheader("Institutional Sentiment")
    try:
        overlay = institutional_sentiment(pipeline.db, symbol)
    except Exception as exc:
        st.error(f"Could not load institutional sentiment: {exc}")
        return

    if not overlay.available or overlay.snapshot is None:
        empty_state(
            f"No stored institutional-sentiment snapshot for {symbol} -- either "
            "this installation has not refreshed since this feature was added, "
            "or TipRanks has no insider/hedge-fund content for this symbol at all.",
            "claudetrade refresh",
        )
        return

    snap = overlay.snapshot
    delta = overlay.delta
    score = overlay.score

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Score", f"{score.score:+.2f}" if score is not None and score.score is not None else "n/a")
        if overlay.score_change is not None:
            st.caption(f"vs prior session: {overlay.score_change:+.2f}")
        st.caption("Not used in scan/backtest scoring -- research overlay only")
    with col2:
        insider_sub = score.insider_subscore if score is not None else None
        st.metric("Insider Axis", f"{insider_sub:+.2f}" if insider_sub is not None else "n/a")
        if score is not None:
            st.caption(
                f"weight {score.insider_weight_applied:.2f}, "
                f"age {score.insider_age_days if score.insider_age_days is not None else 'n/a'}d"
            )
    with col3:
        hf_sub = score.hedge_fund_subscore if score is not None else None
        st.metric("Hedge-Fund Axis", f"{hf_sub:+.2f}" if hf_sub is not None else "n/a")
        if score is not None:
            st.caption(
                f"weight {score.hedge_fund_weight_applied:.2f}, "
                f"age {score.hedge_fund_age_days if score.hedge_fund_age_days is not None else 'n/a'}d"
            )
    with col4:
        flow = format_large_number(snap.insider_net_3m_usd) if snap.insider_net_3m_usd is not None else "n/a"
        st.metric("Net Insider Flow (3m)", flow)
        if delta is not None and delta.has_previous and delta.net_flow_change is not None:
            st.caption(f"vs prior: {format_large_number(delta.net_flow_change)}")

    detail_col1, detail_col2 = st.columns(2)
    with detail_col1:
        confidence = (
            f"{snap.insider_confidence_stock_score:.2f}"
            if snap.insider_confidence_stock_score is not None
            else "n/a"
        )
        st.caption(
            f"Insider confidence (vendor stockScore, 0-1): {confidence} | "
            f"# insiders: {snap.num_of_insiders if snap.num_of_insiders is not None else 'n/a'}"
        )
    with detail_col2:
        hf_sentiment = (
            f"{snap.hedge_fund_sentiment:.2f}" if snap.hedge_fund_sentiment is not None else "n/a"
        )
        st.caption(
            f"Hedge-fund sentiment (vendor, 0-1): {hf_sentiment} | "
            f"market cap: {format_large_number(snap.market_cap_usd)}"
        )

    with st.expander("Recent insider transactions", expanded=False):
        if not snap.recent_insider_transactions:
            st.caption("No individual insider transactions stored for this symbol.")
        else:
            rows = [
                {
                    "Date": t.r_date,
                    "Name": t.name,
                    "Role": (
                        "Officer" if t.is_officer else "Director" if t.is_director else
                        "10% Owner" if t.is_ten_percent_owner else "n/a"
                    ),
                    "Title": t.officer_title,
                    "Operation": t.operation_description,
                    "Shares": t.number_of_shares,
                    "Est. Value": t.estimated_shares_value,
                    "Link": t.link,
                }
                for t in snap.recent_insider_transactions
            ]
            st.dataframe(
                rows,
                hide_index=True,
                width="stretch",
                column_config={
                    "Date": st.column_config.DateColumn(),
                    "Est. Value": st.column_config.NumberColumn(format="$%.0f"),
                    "Link": st.column_config.LinkColumn(),
                },
            )
        st.caption(
            "insiderOperationId/action codes are not documented by TipRanks; the "
            "vendor's own operation description text is trusted for display, the "
            "raw numeric code is never re-labelled -- see docs/api-providers.md."
        )

    with st.expander("Notable hedge-fund holder moves", expanded=False):
        if not snap.notable_holder_moves:
            st.caption("No institutional holder moves stored for this symbol.")
        else:
            rows = [
                {
                    "Manager": m.manager_name,
                    "Institution": m.institution_name,
                    "Effective Date": m.effective_date,
                    "Change": m.change_pct,
                    "Change $": m.change_amount,
                    "% of Portfolio": m.percentage_of_portfolio,
                    "Stars": m.stars,
                    "Active": m.is_active,
                }
                for m in snap.notable_holder_moves
            ]
            st.dataframe(
                rows,
                hide_index=True,
                width="stretch",
                column_config={
                    "Effective Date": st.column_config.DateColumn(),
                    "Change $": st.column_config.NumberColumn(format="$%.0f"),
                    "Stars": st.column_config.NumberColumn(format="%.1f"),
                },
            )
        st.caption(
            "Hedge-fund holdings are SEC 13F-lagged by construction -- the latest "
            "quarter shown here is routinely 1-3 months stale even on the day it "
            "first appears in the vendor feed."
        )


def _render_signal_history(pipeline, signals) -> None:
    st.subheader("Signal History")
    if not signals:
        empty_state("No signal history for this symbol yet.", "claudetrade scan")
        return
    rows = []
    for sig in signals[:25]:
        status = pipeline.ledger.current_status(sig.signal_id)
        rows.append(
            {
                "Session": sig.session,
                "Strategy": sig.strategy,
                "Direction": theme.direction_label(str(sig.direction)),
                "Score": sig.overall_score,
                "Status": format_status(status.value if status else "unknown"),
                "Entry Low": sig.plan.entry_low,
                "Entry High": sig.plan.entry_high,
            }
        )
    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        column_config={
            "Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
            "Entry Low": st.column_config.NumberColumn(format="$%.2f"),
            "Entry High": st.column_config.NumberColumn(format="$%.2f"),
            "Session": st.column_config.DateColumn(),
        },
    )
