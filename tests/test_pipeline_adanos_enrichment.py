"""``Pipeline._enrich_adanos_candidates`` -- the wiring between a completed
scan and ``providers.social.adanos.AdanosProvider.enrich_candidates`` (see
that module's own test suite, ``tests/test_adanos_provider.py
::TestEnrichCandidates``, for the provider-side scope/cap/delay/memo
mechanics this method delegates to), plus ``Pipeline
._store_adanos_enrichment_snapshot`` -- the ``on_snapshot`` callback that
feeds ``db.models.AdanosSnapshotRow`` from a successful enrichment, reusing
the same ``(session, platform, symbol)`` upsert path
``data.ingest.DataIngestor.ingest_adanos`` uses for trending.

This file only exercises the PIPELINE-side wiring: candidate ordering/
de-duplication, the "no provider configured" no-op, that a scan can never
fail because enrichment raised, and that a stored enrichment snapshot is
visible through the exact query ``webapi.attention`` uses for the Screener
grid. Building a full, DB-backed ``Pipeline.scan()`` call that actually
produces signals is heavy machinery (price bars, universe, context building
-- see ``tests/test_signals_funnel.py`` for why engine-level tests call
``SignalEngine.scan`` directly instead) and orthogonal to what this wiring
method needs to prove, so a hand-built ``ScanResult`` is used instead.
"""

from __future__ import annotations

import datetime as dt

from claudetrade.config import AppConfig
from claudetrade.db.models import AdanosSnapshotRow, Security
from claudetrade.db.session import Database
from claudetrade.domain import AdanosSnapshot, MarketRegime, RegimeState
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
    enrich (and whether an ``on_snapshot`` callback was supplied), without
    touching any budget/network/cache machinery -- that machinery is the
    provider's own responsibility and is covered by
    ``tests/test_adanos_provider.py``."""

    name = "adanos"

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dt.date]] = []
        self.on_snapshot = None

    def enrich_candidates(self, symbols: list[str], *, session: dt.date, on_snapshot=None) -> int:
        self.calls.append((list(symbols), session))
        self.on_snapshot = on_snapshot
        return len(symbols)


class _SnapshotProducingAdanos:
    """Stands in for AdanosProvider, additionally simulating one successful
    enrichment by invoking ``on_snapshot`` -- exercises the pipeline's own
    storage wiring (``Pipeline._store_adanos_enrichment_snapshot``) without
    any real network/cache machinery."""

    name = "adanos"

    def __init__(self, snapshot: AdanosSnapshot) -> None:
        self._snapshot = snapshot

    def enrich_candidates(self, symbols: list[str], *, session: dt.date, on_snapshot=None) -> int:
        if on_snapshot is not None and symbols:
            on_snapshot(self._snapshot)
        return 1 if symbols else 0


class _BoomingAdanos:
    """A provider whose ``enrich_candidates`` misbehaves and raises -- the
    exact case ``_enrich_adanos_candidates`` must swallow."""

    name = "adanos"

    def enrich_candidates(self, symbols: list[str], *, session: dt.date, on_snapshot=None) -> int:
        raise RuntimeError("boom")


class TestEnrichAdanosCandidates:
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

        pipeline._enrich_adanos_candidates(_scan_result(signals), SESSION)

        assert len(provider.calls) == 1
        symbols, session_arg = provider.calls[0]
        assert symbols == ["BBB", "AAA", "CCC"]
        assert session_arg == SESSION

    def test_passes_the_full_symbol_list_not_a_pre_capped_slice(
        self, tmp_app_config: AppConfig, tmp_db: Database, make_signal
    ) -> None:
        """Breadth/cap is the PROVIDER's job (``AdanosConfig.enrich_scope``/
        ``enrich_max_symbols_per_scan``) -- the pipeline must hand over every
        distinct signal symbol, best-first, and let the provider decide how
        much of it to use."""
        pipeline = Pipeline(tmp_app_config, tmp_db)
        provider = _RecordingAdanos()
        pipeline.adanos = [provider]

        signals = [
            make_signal(symbol=f"SYM{i}", overall_score=float(100 - i), session=SESSION)
            for i in range(75)
        ]

        pipeline._enrich_adanos_candidates(_scan_result(signals), SESSION)

        symbols, _ = provider.calls[0]
        assert len(symbols) == 75

    def test_supplies_an_on_snapshot_callback(
        self, tmp_app_config: AppConfig, tmp_db: Database, make_signal
    ) -> None:
        pipeline = Pipeline(tmp_app_config, tmp_db)
        provider = _RecordingAdanos()
        pipeline.adanos = [provider]

        pipeline._enrich_adanos_candidates(
            _scan_result([make_signal(symbol="AAA", session=SESSION)]), SESSION
        )

        assert callable(provider.on_snapshot)

    def test_no_adanos_provider_is_a_silent_no_op(
        self, tmp_app_config: AppConfig, tmp_db: Database, make_signal
    ) -> None:
        pipeline = Pipeline(tmp_app_config, tmp_db)
        pipeline.adanos = []

        # Must not raise.
        pipeline._enrich_adanos_candidates(
            _scan_result([make_signal(symbol="AAA", session=SESSION)]), SESSION
        )

    def test_provider_exception_is_swallowed_a_scan_must_never_fail_because_of_it(
        self, tmp_app_config: AppConfig, tmp_db: Database, make_signal
    ) -> None:
        pipeline = Pipeline(tmp_app_config, tmp_db)
        pipeline.adanos = [_BoomingAdanos()]

        # Must not raise, even though the (only) provider's call always does.
        pipeline._enrich_adanos_candidates(
            _scan_result([make_signal(symbol="AAA", session=SESSION)]), SESSION
        )

    def test_empty_signal_list_calls_the_provider_with_no_symbols(
        self, tmp_app_config: AppConfig, tmp_db: Database
    ) -> None:
        pipeline = Pipeline(tmp_app_config, tmp_db)
        provider = _RecordingAdanos()
        pipeline.adanos = [provider]

        pipeline._enrich_adanos_candidates(_scan_result([]), SESSION)

        assert provider.calls == [([], SESSION)]


class TestStoreAdanosEnrichmentSnapshot:
    """``Pipeline._store_adanos_enrichment_snapshot`` -- reuses
    ``db.models.AdanosSnapshotRow``'s ``(session, platform, symbol)`` upsert
    key, the exact same storage contract
    ``data.ingest.DataIngestor.ingest_adanos`` writes for trending rows (see
    ``tests/test_adanos_provider.py::TestIngest`` for that side)."""

    def test_stores_a_row_readable_by_webapi_attention(
        self, tmp_app_config: AppConfig, tmp_db: Database
    ) -> None:
        """The whole point of feeding this table from enrichment: a symbol
        that never appeared in a trending feed still shows up through the
        SAME query ``webapi.attention.latest_attention`` (the Screener
        grid's data source) uses."""
        from claudetrade.webapi.attention import latest_attention

        with tmp_db.session() as db_session:
            db_session.add(Security(symbol="OUTSIDER", name="Outsider Corp"))

        pipeline = Pipeline(tmp_app_config, tmp_db)
        snapshot = AdanosSnapshot(
            symbol="OUTSIDER",
            platform="x",
            company_name="Outsider Corp",
            buzz_score=42.0,
            mentions=17,
            trend="rising",
            sentiment_score=0.25,
            bullish_pct=60.0,
            bearish_pct=40.0,
            engagement=500.0,
            trend_history=[],
        )

        pipeline._store_adanos_enrichment_snapshot(snapshot, SESSION)

        with tmp_db.read_session() as db_session:
            row = (
                db_session.query(AdanosSnapshotRow)
                .filter_by(symbol="OUTSIDER", session=SESSION, platform="x")
                .one()
            )
        assert row.buzz_score == 42.0
        assert row.sentiment_score == 0.25
        assert row.trend == "rising"

        aggregate = latest_attention(tmp_db, ["OUTSIDER"])
        assert "OUTSIDER" in aggregate
        assert aggregate["OUTSIDER"].platforms == ["x"]
        assert aggregate["OUTSIDER"].buzz_score == 42.0

    def test_re_storing_the_same_session_platform_symbol_updates_not_duplicates(
        self, tmp_app_config: AppConfig, tmp_db: Database
    ) -> None:
        with tmp_db.session() as db_session:
            db_session.add(Security(symbol="AAA", name="AAA Inc"))

        pipeline = Pipeline(tmp_app_config, tmp_db)
        pipeline._store_adanos_enrichment_snapshot(
            AdanosSnapshot(symbol="AAA", platform="x", buzz_score=10.0), SESSION
        )
        pipeline._store_adanos_enrichment_snapshot(
            AdanosSnapshot(symbol="AAA", platform="x", buzz_score=90.0), SESSION
        )

        with tmp_db.read_session() as db_session:
            rows = db_session.query(AdanosSnapshotRow).filter_by(symbol="AAA").all()
        assert len(rows) == 1
        assert rows[0].buzz_score == 90.0

    def test_end_to_end_via_enrich_adanos_candidates(
        self, tmp_app_config: AppConfig, tmp_db: Database, make_signal
    ) -> None:
        """The full wiring: ``_enrich_adanos_candidates`` passes an
        ``on_snapshot`` callback down to the provider, and a snapshot the
        provider hands back lands in the database through it."""
        with tmp_db.session() as db_session:
            db_session.add(Security(symbol="ZZZ", name="ZZZ Corp"))

        pipeline = Pipeline(tmp_app_config, tmp_db)
        snapshot = AdanosSnapshot(symbol="ZZZ", platform="reddit", buzz_score=55.0, mentions=9)
        pipeline.adanos = [_SnapshotProducingAdanos(snapshot)]

        pipeline._enrich_adanos_candidates(
            _scan_result([make_signal(symbol="ZZZ", session=SESSION)]), SESSION
        )

        with tmp_db.read_session() as db_session:
            row = (
                db_session.query(AdanosSnapshotRow)
                .filter_by(symbol="ZZZ", session=SESSION, platform="reddit")
                .one()
            )
        assert row.buzz_score == 55.0
        assert row.mentions == 9

    def test_a_storage_failure_never_raises(
        self, tmp_app_config: AppConfig, tmp_db: Database, monkeypatch
    ) -> None:
        """A symbol not present in ``securities`` still stores fine (unlike
        ``ingest_adanos``, this path has no securities-membership guard --
        see the method's docstring for why); this test instead proves a
        genuine storage failure is swallowed, not raised -- a scan must
        never fail because the enrichment-derived snapshot could not be
        written."""
        pipeline = Pipeline(tmp_app_config, tmp_db)

        def _boom(*_a, **_kw):
            raise RuntimeError("db boom")

        monkeypatch.setattr(tmp_db, "session", _boom)

        # Must not raise.
        pipeline._store_adanos_enrichment_snapshot(
            AdanosSnapshot(symbol="AAA", platform="x", buzz_score=1.0), SESSION
        )
