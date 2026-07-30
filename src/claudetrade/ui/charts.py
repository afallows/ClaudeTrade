"""Plotly chart builders for the dark-theme UI.

Design rules followed throughout (see this project's `dataviz` design
skill):

* One shared x-axis per figure, but never two y-axes sharing one plot area
  (no dual-axis charts) -- volume, RSI and sentiment each get their own
  stacked row instead of an overlaid secondary axis.
* Direction/polarity (up/down, long/short, bullish/bearish) always uses the
  same blue/red diverging pair, never an arbitrary categorical colour.
* A legend is present whenever more than one series shares an axis; a single
  series (e.g. one equity line) is named by the chart title instead.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from claudetrade.domain import Bar
from claudetrade.features.indicators import bollinger_bands, rsi, sma
from claudetrade.ui import theme
from claudetrade.ui.data_access import SentimentPoint

_SMA_COLORS: dict[int, str] = {
    10: theme.SEQUENTIAL_BLUE[1],
    20: theme.SEQUENTIAL_BLUE[2],
    50: theme.SEQUENTIAL_BLUE[4],
    100: theme.SEQUENTIAL_BLUE[5],
    200: theme.SEQUENTIAL_BLUE[6],
}


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _empty(message: str = "No data available") -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=message, showarrow=False, font={"color": theme.INK_MUTED, "size": 14}
    )
    fig.update_layout(
        template=theme.PLOTLY_TEMPLATE,
        paper_bgcolor=theme.PAGE_PLANE,
        plot_bgcolor=theme.SURFACE,
        height=200,
    )
    return fig


def _bars_frame(bars: list[Bar]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [b.session for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )


def create_ticker_chart(
    bars: list[Bar],
    *,
    title: str = "Price Action",
    sma_windows: Sequence[int] = (),
    show_bollinger: bool = False,
    show_rsi: bool = False,
    entry_low: float | None = None,
    entry_high: float | None = None,
    stop_loss: float | None = None,
    targets: Sequence[float] = (),
    earnings_dates: Sequence[dt.date] = (),
    sentiment: Sequence[SentimentPoint] = (),
    height: int = 780,
) -> go.Figure:
    """Candlestick + volume, with optional overlays, RSI panel and sentiment panel.

    Each of price, volume, RSI and sentiment gets its own row on a shared
    x-axis -- never a second y-axis squeezed onto the price plot.
    """
    if not bars:
        return _empty("No price history stored for this symbol yet")

    df = _bars_frame(bars)
    close = pd.Series(df["close"].to_numpy(), index=pd.to_datetime(df["date"]))

    row_specs: list[str] = ["price", "volume"]
    if show_rsi:
        row_specs.append("rsi")
    if sentiment:
        row_specs.append("sentiment")

    heights = {"price": 0.55, "volume": 0.15, "rsi": 0.15, "sentiment": 0.15}
    row_heights = [heights[r] for r in row_specs]
    titles = {
        "price": "",
        "volume": "Volume",
        "rsi": "RSI (14)",
        "sentiment": "Mentions &amp; Sentiment",
    }

    fig = make_subplots(
        rows=len(row_specs),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=row_heights,
        subplot_titles=[titles[r] for r in row_specs],
    )
    price_row = row_specs.index("price") + 1
    volume_row = row_specs.index("volume") + 1

    fig.add_trace(
        go.Candlestick(
            x=df["date"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="OHLC",
            increasing_line_color=theme.LONG_COLOR,
            decreasing_line_color=theme.SHORT_COLOR,
        ),
        row=price_row,
        col=1,
    )

    for window in sma_windows:
        line = sma(close, window)
        fig.add_trace(
            go.Scatter(
                x=df["date"],
                y=line.to_numpy(),
                mode="lines",
                name=f"SMA {window}",
                line={"color": _SMA_COLORS.get(window, theme.INK_MUTED), "width": 1.5},
            ),
            row=price_row,
            col=1,
        )

    if show_bollinger:
        bands = bollinger_bands(close, 20, 2.0)
        for col_name, label in (("upper", "Bollinger Upper"), ("lower", "Bollinger Lower")):
            fig.add_trace(
                go.Scatter(
                    x=df["date"],
                    y=bands[col_name].to_numpy(),
                    mode="lines",
                    name=label,
                    line={"color": theme.INK_MUTED, "width": 1, "dash": "dot"},
                ),
                row=price_row,
                col=1,
            )

    if entry_low is not None and entry_high is not None:
        fig.add_hrect(
            y0=entry_low,
            y1=entry_high,
            fillcolor=theme.ACCENT,
            opacity=0.15,
            line_width=0,
            row=price_row,
            col=1,
            annotation_text="Entry zone",
            annotation_position="top left",
            annotation_font_size=10,
            annotation_font_color=theme.INK_SECONDARY,
        )
    if stop_loss is not None:
        fig.add_hline(
            y=stop_loss,
            line={"color": theme.STATUS_CRITICAL, "width": 1.5, "dash": "dash"},
            annotation_text="Stop",
            annotation_font_size=10,
            annotation_font_color=theme.STATUS_CRITICAL,
            row=price_row,
            col=1,
        )
    for i, target in enumerate(targets, start=1):
        fig.add_hline(
            y=target,
            line={"color": theme.STATUS_GOOD, "width": 1.5, "dash": "dash"},
            annotation_text=f"T{i}",
            annotation_font_size=10,
            annotation_font_color=theme.STATUS_GOOD,
            row=price_row,
            col=1,
        )

    chart_start, chart_end = df["date"].min(), df["date"].max()
    for report_date in earnings_dates:
        if chart_start <= report_date <= chart_end:
            fig.add_vline(
                x=pd.Timestamp(report_date),
                line={"color": theme.INK_MUTED, "width": 1, "dash": "dot"},
                row=price_row,
                col=1,
            )

    volume_colors = [
        theme.LONG_COLOR if c >= o else theme.SHORT_COLOR
        for o, c in zip(df["open"], df["close"], strict=True)
    ]
    fig.add_trace(
        go.Bar(x=df["date"], y=df["volume"], name="Volume", marker_color=volume_colors, opacity=0.75, showlegend=False),
        row=volume_row,
        col=1,
    )

    if show_rsi:
        rsi_row = row_specs.index("rsi") + 1
        rsi_line = rsi(close, 14)
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=rsi_line.to_numpy(), mode="lines", name="RSI",
                line={"color": theme.ACCENT, "width": 1.5}, showlegend=False,
            ),
            row=rsi_row,
            col=1,
        )
        for level in (30, 70):
            fig.add_hline(
                y=level,
                line={"color": theme.GRIDLINE, "width": 1, "dash": "dot"},
                row=rsi_row,
                col=1,
            )
        fig.update_yaxes(range=[0, 100], row=rsi_row, col=1)

    if sentiment:
        sent_row = row_specs.index("sentiment") + 1
        sent_dates = [p.session for p in sentiment]
        sent_counts = [p.post_count for p in sentiment]
        sent_colors = [
            theme.LONG_COLOR if p.bull_bear_ratio >= 1.0 else theme.SHORT_COLOR for p in sentiment
        ]
        fig.add_trace(
            go.Bar(
                x=sent_dates,
                y=sent_counts,
                name="Mentions (blue=bullish, red=bearish)",
                marker_color=sent_colors,
                showlegend=False,
            ),
            row=sent_row,
            col=1,
        )

    fig.update_layout(
        title=title,
        template=theme.PLOTLY_TEMPLATE,
        paper_bgcolor=theme.PAGE_PLANE,
        plot_bgcolor=theme.SURFACE,
        xaxis_rangeslider_visible=False,
        height=height,
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.02, "yanchor": "bottom"},
        margin={"l": 50, "r": 30, "t": 60, "b": 30},
    )
    fig.update_xaxes(gridcolor=theme.GRIDLINE, showgrid=False)
    fig.update_yaxes(gridcolor=theme.GRIDLINE, zeroline=False)
    return fig


def create_sparkline(
    sessions: Sequence[dt.date], values: Sequence[float], *, height: int = 90
) -> go.Figure:
    """Minimal, axis-free line -- for a compact equity trend in a dashboard tile."""
    if not sessions or not values:
        return _empty("No history yet")
    color = theme.LONG_COLOR if values[-1] >= values[0] else theme.SHORT_COLOR
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=list(sessions),
            y=list(values),
            mode="lines",
            line={"color": color, "width": 2},
            fill="tozeroy",
            fillcolor=_hex_to_rgba(color, 0.15),
            hoverinfo="skip",
        )
    )
    fig.update_layout(
        template=theme.PLOTLY_TEMPLATE,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=height,
        margin={"l": 0, "r": 0, "t": 4, "b": 4},
        showlegend=False,
    )
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def create_equity_curve_chart(
    sessions: list[dt.date],
    equity: list[float],
    title: str = "Equity Curve",
    height: int = 380,
) -> go.Figure:
    """Account/backtest equity over time."""
    if not sessions or not equity:
        return _empty()

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=sessions,
            y=equity,
            mode="lines",
            name="Equity",
            fill="tozeroy",
            line={"color": theme.ACCENT, "width": 2},
            fillcolor="rgba(57,135,229,0.12)",
        )
    )
    fig.update_layout(
        title=title,
        yaxis_title="Equity ($)",
        xaxis_title="",
        height=height,
        hovermode="x unified",
        template=theme.PLOTLY_TEMPLATE,
        paper_bgcolor=theme.PAGE_PLANE,
        plot_bgcolor=theme.SURFACE,
        showlegend=False,
        margin={"l": 50, "r": 30, "t": 50, "b": 30},
    )
    fig.update_xaxes(gridcolor=theme.GRIDLINE)
    fig.update_yaxes(gridcolor=theme.GRIDLINE)
    return fig


def create_drawdown_chart(
    sessions: list[dt.date],
    drawdowns: list[float],
    title: str = "Drawdown",
    height: int = 260,
) -> go.Figure:
    """Running drawdown (%) -- always an adverse-excursion series, so it uses
    the diverging pair's 'short/loss' pole rather than the accent colour."""
    if not sessions or not drawdowns:
        return _empty()

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=sessions,
            y=[-abs(d) for d in drawdowns],
            fill="tozeroy",
            name="Drawdown",
            line={"color": theme.SHORT_COLOR, "width": 2},
            fillcolor="rgba(230,103,103,0.18)",
        )
    )
    fig.update_layout(
        title=title,
        yaxis_title="Drawdown (%)",
        xaxis_title="",
        height=height,
        hovermode="x unified",
        template=theme.PLOTLY_TEMPLATE,
        paper_bgcolor=theme.PAGE_PLANE,
        plot_bgcolor=theme.SURFACE,
        showlegend=False,
        margin={"l": 50, "r": 30, "t": 50, "b": 30},
    )
    fig.update_xaxes(gridcolor=theme.GRIDLINE)
    fig.update_yaxes(gridcolor=theme.GRIDLINE)
    return fig


def create_funnel_bar_chart(
    labels: list[str], values: list[float], title: str = "Rejection Funnel", height: int = 420
) -> go.Figure:
    """Horizontal bar chart of funnel stage counts, most-upstream stage on top."""
    if not labels or not values:
        return _empty()

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker_color=theme.ACCENT,
        )
    )
    fig.update_layout(
        title=title,
        xaxis_title="Count",
        height=height,
        template=theme.PLOTLY_TEMPLATE,
        paper_bgcolor=theme.PAGE_PLANE,
        plot_bgcolor=theme.SURFACE,
        showlegend=False,
        margin={"l": 180, "r": 30, "t": 50, "b": 30},
        yaxis={"autorange": "reversed"},
    )
    fig.update_xaxes(gridcolor=theme.GRIDLINE)
    return fig


__all__ = [
    "create_drawdown_chart",
    "create_equity_curve_chart",
    "create_funnel_bar_chart",
    "create_sparkline",
    "create_ticker_chart",
]
