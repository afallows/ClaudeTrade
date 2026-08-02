"""Unit tests for ``claudetrade.ui.components.stats``'s rendering logic.

``st.metric``/``st.warning`` are safe no-ops outside a running Streamlit
script (see the module docstring in ``claudetrade.ui.state``); these tests
monkeypatch them to capture the arguments each helper actually passes, so the
"unavailable-with-reason" branch is verified precisely rather than just
"doesn't raise".
"""

from __future__ import annotations

import claudetrade.ui.components.stats as stats_module


def test_metric_tile_renders_value_when_available(monkeypatch):
    calls = []
    monkeypatch.setattr(stats_module.st, "metric", lambda *a, **k: calls.append((a, k)))

    stats_module.metric_tile("Sharpe", "1.23", help_text="annualised")

    (args, kwargs) = calls[0]
    assert args[0] == "Sharpe"
    assert args[1] == "1.23"
    assert kwargs.get("help") == "annualised"


def test_metric_tile_renders_unavailable_with_reason(monkeypatch):
    calls = []
    monkeypatch.setattr(stats_module.st, "metric", lambda *a, **k: calls.append((a, k)))

    stats_module.metric_tile("Sharpe", None, unavailable_reason="insufficient_sample: 2 observations")

    (args, kwargs) = calls[0]
    assert args[1] == "unavailable"
    assert "insufficient_sample" in kwargs.get("help", "")


def test_metric_tile_none_value_without_explicit_reason_still_unavailable(monkeypatch):
    calls = []
    monkeypatch.setattr(stats_module.st, "metric", lambda *a, **k: calls.append((a, k)))

    stats_module.metric_tile("Max Drawdown", None)

    (args, kwargs) = calls[0]
    assert args[1] == "unavailable"
    assert kwargs.get("help")


def test_significance_caveat_silent_when_significant(monkeypatch):
    calls = []
    monkeypatch.setattr(stats_module.st, "warning", lambda *a, **k: calls.append(a))

    stats_module.significance_caveat(is_significant=True, reason=None, trade_count=100)

    assert calls == []


def test_significance_caveat_warns_with_reason_and_count(monkeypatch):
    calls = []
    monkeypatch.setattr(stats_module.st, "warning", lambda *a, **k: calls.append(a))

    stats_module.significance_caveat(
        is_significant=False, reason="trade_count_below_floor: 3, below 30", trade_count=3
    )

    assert len(calls) == 1
    message = calls[0][0]
    assert "NOT STATISTICALLY SIGNIFICANT" in message
    assert "3 completed trade(s)" in message
    assert "trade_count_below_floor" in message
