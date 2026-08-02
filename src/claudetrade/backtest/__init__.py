"""Backtest layer: deterministic trade simulation and performance analysis.

This module turns daily OHLCV data through strategy signals into a complete
account P&L with equity curve and trade-by-trade metrics. Every design choice
prioritises honest win/loss accounting over flattering backtests.
"""

from __future__ import annotations

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "ContextProvider",
    "DictContextProvider",
    "chronological_split",
    "equity_to_dataframe",
    "export_csv",
    "export_excel",
    "metrics_to_dataframe",
    "multi_objective_score",
    "parameter_sensitivity",
    "render_markdown_report",
    "trades_to_dataframe",
    "walk_forward",
]
