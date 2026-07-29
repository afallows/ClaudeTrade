"""Chart utilities using Plotly for dark-theme compatibility.

Builds candlestick charts, equity curves, drawdown charts, and indicator
overlays. All charts are responsive and use the operator's theme preference.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import plotly.graph_objects as go

from claudetrade.domain import Bar


def create_candlestick_chart(
    bars: list[Bar],
    title: str = "Price Action",
    height: int = 600,
) -> go.Figure:
    """Create a candlestick chart with volume subplot.

    Args:
        bars: List of Bar objects, oldest first.
        title: Chart title.
        height: Chart height in pixels.

    Returns:
        A Plotly figure with candlestick and volume.
    """
    if not bars:
        return go.Figure().add_annotation(text="No data available")

    df = pd.DataFrame([
        {
            "date": bar.session,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ])

    fig = go.Figure()

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=df["date"],
        open=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        name="OHLC",
        increasing_line_color="green",
        decreasing_line_color="red",
    ))

    # Volume bars
    colors = ["green" if df.iloc[i]["close"] >= df.iloc[i]["open"] else "red"
              for i in range(len(df))]
    fig.add_trace(go.Bar(
        x=df["date"],
        y=df["volume"],
        name="Volume",
        marker_color=colors,
        opacity=0.3,
        yaxis="y2",
    ))

    fig.update_layout(
        title=title,
        yaxis_title="Price",
        yaxis2={
            "title": "Volume",
            "overlaying": "y",
            "side": "right",
        },
        xaxis_rangeslider_visible=False,
        height=height,
        hovermode="x unified",
        template="plotly_dark",
        margin={"l": 50, "r": 50, "t": 80, "b": 50},
    )

    return fig


def create_equity_curve_chart(
    sessions: list[dt.date],
    equity: list[float],
    title: str = "Equity Curve",
    height: int = 400,
) -> go.Figure:
    """Create an equity curve chart.

    Args:
        sessions: Trading dates.
        equity: Equity values per session.
        title: Chart title.
        height: Chart height in pixels.

    Returns:
        A Plotly figure.
    """
    if not sessions or not equity:
        return go.Figure().add_annotation(text="No data available")

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=sessions,
        y=equity,
        mode="lines",
        name="Equity",
        fill="tozeroy",
        line={"color": "rgba(0,100,200,1)", "width": 2},
    ))

    fig.update_layout(
        title=title,
        yaxis_title="Equity ($)",
        xaxis_title="Date",
        height=height,
        hovermode="x unified",
        template="plotly_dark",
        margin={"l": 50, "r": 50, "t": 80, "b": 50},
    )

    return fig


def create_drawdown_chart(
    sessions: list[dt.date],
    drawdowns: list[float],
    title: str = "Drawdown",
    height: int = 300,
) -> go.Figure:
    """Create a drawdown (running max loss) chart.

    Args:
        sessions: Trading dates.
        drawdowns: Drawdown percentages (negative values).
        title: Chart title.
        height: Chart height in pixels.

    Returns:
        A Plotly figure.
    """
    if not sessions or not drawdowns:
        return go.Figure().add_annotation(text="No data available")

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=sessions,
        y=drawdowns,
        fill="tozeroy",
        name="Drawdown",
        line={"color": "rgba(255,0,0,1)", "width": 2},
    ))

    fig.update_layout(
        title=title,
        yaxis_title="Drawdown (%)",
        xaxis_title="Date",
        height=height,
        hovermode="x unified",
        template="plotly_dark",
        margin={"l": 50, "r": 50, "t": 80, "b": 50},
    )

    return fig


def create_bar_chart(
    labels: list[str],
    values: list[float],
    title: str = "Metrics",
    yaxis_title: str = "Value",
    height: int = 400,
) -> go.Figure:
    """Create a bar chart for metrics.

    Args:
        labels: Category labels.
        values: Values.
        title: Chart title.
        yaxis_title: Y-axis label.
        height: Chart height.

    Returns:
        A Plotly figure.
    """
    if not labels or not values:
        return go.Figure().add_annotation(text="No data available")

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels,
        y=values,
        marker_color="rgba(0,150,200,0.7)",
    ))

    fig.update_layout(
        title=title,
        yaxis_title=yaxis_title,
        height=height,
        template="plotly_dark",
        margin={"l": 50, "r": 50, "t": 80, "b": 50},
        showlegend=False,
    )

    return fig


def create_scatter_chart(
    x: list[float],
    y: list[float],
    labels: list[str] | None = None,
    title: str = "Scatter",
    xaxis_title: str = "X",
    yaxis_title: str = "Y",
    height: int = 500,
) -> go.Figure:
    """Create a scatter plot.

    Args:
        x: X values.
        y: Y values.
        labels: Optional text labels for each point.
        title: Chart title.
        xaxis_title: X-axis label.
        yaxis_title: Y-axis label.
        height: Chart height.

    Returns:
        A Plotly figure.
    """
    if not x or not y:
        return go.Figure().add_annotation(text="No data available")

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x,
        y=y,
        mode="markers+text" if labels else "markers",
        text=labels,
        textposition="top center",
        marker={
            "size": 8,
            "color": y,
            "colorscale": "Viridis",
            "showscale": True,
            "colorbar": {"title": "Value"},
        },
    ))

    fig.update_layout(
        title=title,
        xaxis_title=xaxis_title,
        yaxis_title=yaxis_title,
        height=height,
        hovermode="closest",
        template="plotly_dark",
        margin={"l": 50, "r": 50, "t": 80, "b": 50},
    )

    return fig
