"""Unit tests for the pure ``claudetrade.webapi.serialize`` helpers -- no DB,
no FastAPI app, no HTTP.
"""

from __future__ import annotations

import datetime as dt

from claudetrade.domain import Bar, Direction, SignalStatus
from claudetrade.webapi.serialize import (
    active_signal_for,
    compute_indicators,
    regime_label,
    signal_to_detail,
    signal_to_row,
    top_candidates,
)


def _bar(i: int) -> Bar:
    price = 100.0 + i
    return Bar(
        symbol="TEST",
        session=dt.date(2024, 1, 1) + dt.timedelta(days=i),
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1_000_000,
    )


def test_compute_indicators_empty_bars_returns_empty_lists():
    out = compute_indicators([])
    assert out.sma_20 == []
    assert out.rsi_14 == []


def test_compute_indicators_warms_up_then_produces_values():
    bars = [_bar(i) for i in range(25)]
    out = compute_indicators(bars)
    assert len(out.sma_20) == 25
    # First 19 rows are warm-up (None); the 20th onward is a real average.
    assert all(v is None for v in out.sma_20[:19])
    assert out.sma_20[19] is not None
    assert all(v is None for v in out.sma_200)


def test_regime_label_known_and_unknown_values():
    assert regime_label("bull_quiet") == "Bull -- Quiet"
    assert regime_label(None) == "Unknown"
    assert regime_label("something_new") == "Something_New"


def test_signal_to_row_shapes_flat_fields(make_signal):
    sig = make_signal(symbol="AAA", overall_score=72.5, confidence=0.6)
    row = signal_to_row(sig, SignalStatus.ACTIONABLE)
    assert row.symbol == "AAA"
    assert row.status == "actionable"
    assert row.direction == "long"
    assert row.reward_risk_ratio == sig.plan.reward_risk_ratio


def test_signal_to_row_status_unknown_when_ledger_has_no_revision(make_signal):
    sig = make_signal(symbol="CCC")
    row = signal_to_row(sig, None)
    assert row.status == "unknown"


def test_signal_to_detail_includes_components_and_plan(make_signal):
    sig = make_signal(symbol="BBB")
    detail = signal_to_detail(sig, None)
    assert detail.status == "unknown"
    assert detail.components.technical_setup == sig.components.technical_setup
    assert detail.plan.entry_low == sig.plan.entry_low
    assert detail.thesis == sig.thesis


def test_active_signal_for_empty_list_is_none():
    assert active_signal_for([]) is None


def test_active_signal_for_prefers_tradable_over_newer_non_tradable(make_signal):
    older_tradable = make_signal(
        symbol="A",
        status=SignalStatus.ACTIONABLE,
        session=dt.date(2024, 1, 1),
    )
    newer_expired = make_signal(
        symbol="A",
        status=SignalStatus.EXPIRED,
        session=dt.date(2024, 2, 1),
    )
    assert active_signal_for([older_tradable, newer_expired]) is older_tradable


def test_active_signal_for_falls_back_to_newest_when_none_tradable(make_signal):
    first = make_signal(symbol="A", status=SignalStatus.EXPIRED, session=dt.date(2024, 1, 1))
    second = make_signal(symbol="A", status=SignalStatus.REJECTED, session=dt.date(2024, 2, 1))
    assert active_signal_for([first, second]) is second


def test_top_candidates_ranks_by_score_and_filters_direction(make_signal):
    signals = [
        make_signal(symbol="A", direction=Direction.LONG, overall_score=50.0),
        make_signal(symbol="B", direction=Direction.LONG, overall_score=90.0),
        make_signal(symbol="C", direction=Direction.SHORT, overall_score=99.0),
    ]
    top_longs = top_candidates(signals, "long")
    assert [s.symbol for s in top_longs] == ["B", "A"]


def test_top_candidates_respects_n(make_signal):
    signals = [make_signal(symbol=f"S{i}", overall_score=float(i)) for i in range(10)]
    top = top_candidates(signals, "long", n=3)
    assert len(top) == 3
    assert top[0].symbol == "S9"
