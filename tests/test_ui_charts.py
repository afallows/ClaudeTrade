"""Unit tests for ``claudetrade.ui.charts``'s Plotly figure builders.

Assertions focus on structure (trace count, subplot rows, empty-state
fallback) rather than pixel output -- the properties a regression could
plausibly break.
"""

from __future__ import annotations

import datetime as dt

import plotly.graph_objects as go

from claudetrade.ui.charts import (
    create_drawdown_chart,
    create_equity_curve_chart,
    create_funnel_bar_chart,
    create_sparkline,
    create_ticker_chart,
)
from claudetrade.ui.data_access import SentimentPoint


def test_ticker_chart_empty_bars_returns_annotated_placeholder():
    fig = create_ticker_chart([])
    assert isinstance(fig, go.Figure)
    assert fig.layout.annotations[0].text


def test_ticker_chart_has_candlestick_and_volume_rows(sample_bars):
    fig = create_ticker_chart(sample_bars)
    trace_types = {type(t).__name__ for t in fig.data}
    assert "Candlestick" in trace_types
    assert "Bar" in trace_types  # volume


def test_ticker_chart_adds_one_line_per_sma_window(sample_bars):
    fig = create_ticker_chart(sample_bars, sma_windows=(10, 20))
    sma_traces = [t for t in fig.data if getattr(t, "name", "").startswith("SMA")]
    assert {t.name for t in sma_traces} == {"SMA 10", "SMA 20"}


def test_ticker_chart_bollinger_adds_two_band_traces(sample_bars):
    fig = create_ticker_chart(sample_bars, show_bollinger=True)
    names = {t.name for t in fig.data}
    assert "Bollinger Upper" in names
    assert "Bollinger Lower" in names


def test_ticker_chart_rsi_panel_adds_row_and_reference_lines(sample_bars):
    fig = create_ticker_chart(sample_bars, show_rsi=True)
    rsi_traces = [t for t in fig.data if getattr(t, "name", "") == "RSI"]
    assert len(rsi_traces) == 1
    # Two dotted 30/70 reference lines plus the RSI trace itself.
    assert len(fig.layout.shapes) >= 2


def test_ticker_chart_without_rsi_has_no_rsi_trace(sample_bars):
    fig = create_ticker_chart(sample_bars, show_rsi=False)
    assert not any(getattr(t, "name", "") == "RSI" for t in fig.data)


def test_ticker_chart_sentiment_panel_colors_by_polarity(sample_bars):
    sentiment = [
        SentimentPoint(
            session=sample_bars[0].session,
            post_count=10,
            unique_authors=4,
            engagement_weighted=1.0,
            bull_bear_ratio=2.0,  # bullish -> long colour
            manipulation_risk=0.1,
            confidence=0.8,
        ),
        SentimentPoint(
            session=sample_bars[1].session,
            post_count=5,
            unique_authors=2,
            engagement_weighted=1.0,
            bull_bear_ratio=0.3,  # bearish -> short colour
            manipulation_risk=0.1,
            confidence=0.8,
        ),
    ]
    fig = create_ticker_chart(sample_bars, sentiment=sentiment)
    sentiment_trace = next(t for t in fig.data if "Mentions" in getattr(t, "name", ""))
    colors = list(sentiment_trace.marker.color)
    assert colors[0] != colors[1]


def test_ticker_chart_without_sentiment_has_no_mentions_trace(sample_bars):
    fig = create_ticker_chart(sample_bars, sentiment=[])
    assert not any("Mentions" in getattr(t, "name", "") for t in fig.data)


def test_ticker_chart_entry_stop_target_lines(sample_bars):
    fig = create_ticker_chart(
        sample_bars, entry_low=95.0, entry_high=105.0, stop_loss=90.0, targets=[120.0, 130.0]
    )
    # One hrect (entry zone) + one hline (stop) + two hlines (targets) + any RSI/earnings lines (none here).
    assert len(fig.layout.shapes) >= 4


def test_sparkline_empty_returns_placeholder():
    fig = create_sparkline([], [])
    assert isinstance(fig, go.Figure)


def test_sparkline_color_reflects_direction():
    sessions = [dt.date(2024, 1, 1), dt.date(2024, 1, 2)]
    up = create_sparkline(sessions, [100.0, 110.0])
    down = create_sparkline(sessions, [100.0, 90.0])
    assert up.data[0].line.color != down.data[0].line.color


def test_equity_curve_chart_empty_and_populated():
    assert isinstance(create_equity_curve_chart([], []), go.Figure)
    fig = create_equity_curve_chart([dt.date(2024, 1, 1), dt.date(2024, 1, 2)], [100.0, 105.0])
    assert len(fig.data) == 1


def test_drawdown_chart_values_are_non_positive():
    fig = create_drawdown_chart([dt.date(2024, 1, 1), dt.date(2024, 1, 2)], [0.0, 5.0])
    assert list(fig.data[0].y) == [0.0, -5.0]


def test_funnel_bar_chart_empty_and_populated():
    assert isinstance(create_funnel_bar_chart([], []), go.Figure)
    fig = create_funnel_bar_chart(["Universe", "Signals"], [100, 5])
    assert list(fig.data[0].x) == [100, 5]
