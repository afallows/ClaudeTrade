"""``Pipeline._enrich_adanos_top_candidates`` -- the wiring between a
completed scan and ``providers.social.adanos.AdanosProvider
.enrich_top_candidates`` (see that module's own test suite,
``tests/test_adanos_provider.py::TestEnrichTopCandidates``, for the
provider-side budget/cache mechanics this method delegates to).

This file only exercises the PIPELINE-side wiring: candidate ordering/
de-duplication, the "no provider configured" no-op, and -- the one
non-negotiable property -- that a scan can never fail because enrichment
raised. Building a full, DB-backed ``Pipeline.scan()`` call that actually
produces signals is heavy machinery (price bars, universe, context building
-- see ``tests/test_signals_funnel.py`` for why engine-level tests call
``SignalEngine.scan`` directly instead) and orthogonal to what this wiring
method needs to prove, so a hand-built ``ScanResult`` is used instead.
"""

from __future__ import annotations

import datetime as dt

from claudetrade.config import AppConfig
from claudetrade.db.session import Database
from claudetrade.domain import MarketRegime, RegimeState
from claudetrade.pipeline import Pipeline
from claudetrade.signals.engine import ScanResult
from claudetrade.utils.timeutils import utc_now

SESSION = dt.date(2026, 8, 3)


def _scan_result(signals: list) -> ScanResult:
    return ScanResult(
        session=SESSION,
        generated_at=utc_now(),
        regime=RegimeState(session=SESSION, regime=MarketRegime.NEUTRAL),
        signals=signals,
    )


class _RecordingAdanos:
    """Stands in for AdanosProvider: records exactly what it was asked to
    enrich, without touching any budget/network/cache machinery -- that
    machinery is the provider's own responsibility and is covered by
    ``tests/test_adanos_provider.py``."""

    name = "adanos"

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dt.date]] = []

    def enrich_top_candidates(self, symbols: list[str], *, session: dt.date) -> int:
        self.calls.append((list(symbols), session))
        return len(symbols)


class _BoomingAdanos:
    """A provider whose ``enrich_top_candidates`` misbehaves and raises --
    the exact case ``_enrich_adanos_top_candidates`` must swallow."""

    name = "adanos"

    def enrich_top_candidates(self, symbols: list[str], *, session: dt.date) -> int:
        raise RuntimeError("boom")


class TestEnrichAdanosTopCandidates:
    def test_orders_best_score_first_and_deduplicates_by_symbol(
        self, tmp_app_config: AppConfig, tmp_db: Database, make_signal
    ) -> None:
        pipeline = Pipeline(tmp_app_config, tmp_db)
        provider = _RecordingAdanos()
        pipeline.adanos = [provider]

        signals = [
            make_signal(symbol="AAA", overall_score=50.0, session=SESSION),
            make_signal(symbol="BBB", overall_score=90.0, session=SESSION),
            # A second, lower-scoring AAA signal (different strategy) --
            # AAA must still appear exactly once, at its best-score slot.
            make_signal(
                symbol="AAA", overall_score=70.0, session=SESSION, strategy="other_strategy"
            ),
            make_signal(symbol="CCC", overall_score=60.0, session=SESSION),
        ]

        pipeline._enrich_adanos_top_candidates(_scan_result(signals), SESSION)

        assert len(provider.calls) == 1
        symbols, session_arg = provider.calls[0]
        assert symbols == ["BBB", "AAA", "CCC"]
        assert session_arg == SESSION

    def test_no_adanos_provider_is_a_silent_no_op(
        self, tmp_app_config: AppConfig, tmp_db: Database, make_signal
    ) -> None:
        pipeline = Pipeline(tmp_app_config, tmp_db)
        pipeline.adanos = []

        # Must not raise.
        pipeline._enrich_adanos_top_candidates(
            _scan_result([make_signal(symbol="AAA", session=SESSION)]), SESSION
        )

    def test_provider_exception_is_swallowed_a_scan_must_never_fail_because_of_it(
        self, tmp_app_config: AppConfig, tmp_db: Database, make_signal
    ) -> None:
        pipeline = Pipeline(tmp_app_config, tmp_db)
        pipeline.adanos = [_BoomingAdanos()]

        # Must not raise, even though the (only) provider's call always does.
        pipeline._enrich_adanos_top_candidates(
            _scan_result([make_signal(symbol="AAA", session=SESSION)]), SESSION
        )

    def test_empty_signal_list_calls_the_provider_with_no_symbols(
        self, tmp_app_config: AppConfig, tmp_db: Database
    ) -> None:
        pipeline = Pipeline(tmp_app_config, tmp_db)
        provider = _RecordingAdanos()
        pipeline.adanos = [provider]

        pipeline._enrich_adanos_top_candidates(_scan_result([]), SESSION)

        assert provider.calls == [([], SESSION)]
