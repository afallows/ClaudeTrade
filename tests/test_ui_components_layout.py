"""Unit tests for ``claudetrade.ui.components.layout``'s pure helpers."""

from __future__ import annotations

import datetime as dt

from claudetrade.providers.base import ProviderStatus
from claudetrade.ui.components.layout import latest_regime, summarize_providers


def test_summarize_providers_all_ok():
    statuses = [
        ProviderStatus(name="market", kind="market", available=True, configured=True),
        ProviderStatus(name="ai", kind="ai", available=True, configured=True),
    ]
    summary = summarize_providers(statuses)
    assert summary.all_ok
    assert summary.available == 2
    assert summary.degraded == []


def test_summarize_providers_reports_degraded_names():
    statuses = [
        ProviderStatus(name="market", kind="market", available=True, configured=True),
        ProviderStatus(name="reddit", kind="social", available=False, configured=False),
    ]
    summary = summarize_providers(statuses)
    assert not summary.all_ok
    assert summary.available == 1
    assert summary.degraded == ["reddit"]


def test_summarize_providers_empty_list_not_all_ok():
    summary = summarize_providers([])
    assert summary.total == 0
    assert not summary.all_ok


def test_latest_regime_empty_signals_is_unknown():
    regime, session = latest_regime([])
    assert regime == "unknown"
    assert session is None


def test_latest_regime_picks_most_recently_created(make_signal):
    older = make_signal(session=dt.date(2024, 1, 2))
    newer = make_signal(session=dt.date(2024, 3, 1))
    regime, session = latest_regime([older, newer])
    assert session == dt.date(2024, 3, 1)
    assert regime == "bull_quiet"
