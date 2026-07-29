"""End-to-end CLI tests for the paper-trading execution lifecycle.

Exercises `claudetrade paper open/process/close` through Typer's ``CliRunner``
against a real (tmp-path) SQLite database with a recorded signal and stored
price bars -- the same path a Windows user goes through: `scan` records a
signal, `paper open` submits it through the ``BrokerProvider`` seam, `paper
process` advances it against fresh bars, `paper close` exits it manually.
"""

from __future__ import annotations

import datetime as dt

import pytest
from typer.testing import CliRunner

from claudetrade.cli import app
from claudetrade.config import AppConfig, reset_config_cache
from claudetrade.db.migrations import init_database
from claudetrade.db.models import PriceBar
from claudetrade.db.session import Database, reset_database_cache
from claudetrade.domain import (
    ComponentScores,
    Direction,
    Signal,
    SignalStatus,
    TradePlan,
)
from claudetrade.paper.portfolio import PaperPortfolio
from claudetrade.signals.ledger import SignalLedger

runner = CliRunner()


def make_signal(
    *,
    signal_id: str = "SIG-CLI-1",
    symbol: str = "TEST",
    session: dt.date = dt.date(2023, 1, 3),
    entry_low: float = 99.0,
    entry_high: float = 101.0,
    stop_loss: float = 95.0,
    targets: list[float] | None = None,
    shares: int = 10,
) -> Signal:
    return Signal(
        signal_id=signal_id,
        created_at=dt.datetime(2023, 1, 3, 15, 0, tzinfo=dt.UTC),
        session=session,
        symbol=symbol,
        company_name="Test Co",
        strategy="test",
        direction=Direction.LONG,
        status=SignalStatus.ACTIONABLE,
        reference_price=100.0,
        price_as_of=dt.datetime(2023, 1, 3, 15, 0, tzinfo=dt.UTC),
        overall_score=75.0,
        confidence=0.8,
        components=ComponentScores(),
        plan=TradePlan(
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            targets=targets or [110.0],
            shares=shares,
        ),
    )


def add_bar(
    db: Database,
    *,
    symbol: str = "TEST",
    session: dt.date,
    open_: float = 100.0,
    high: float = 101.5,
    low: float = 99.0,
    close: float = 100.5,
    volume: float = 1_000_000,
) -> None:
    with db.session() as db_session:
        db_session.add(
            PriceBar(
                symbol=symbol,
                session=session,
                open=open_,
                high=high,
                low=low,
                close=close,
                adj_close=close,
                volume=volume,
                source="test",
            )
        )


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """A fresh, migrated database at the exact URL the CLI's own config resolves to.

    ``claudetrade paper ...`` reloads ``AppConfig`` from the environment on
    every invocation (see ``cli._load``) and opens the database through the
    process-wide ``get_database`` cache -- both must be reset so each test is
    isolated and both must agree on where the database lives, which is why
    this builds the ``Database`` at ``cfg.database_url()`` rather than an
    arbitrary path.
    """
    monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
    reset_config_cache()
    reset_database_cache()

    cfg = AppConfig()
    cfg.logging.console = False
    db = Database(cfg.database_url(), config=cfg)
    init_database(db)

    yield cfg, db

    db.dispose()
    reset_database_cache()
    reset_config_cache()


class TestPaperOpen:
    def test_open_fills_signal_through_broker_seam(self, cli_env):
        cfg, db = cli_env
        signal = make_signal()
        SignalLedger(db).record(signal)
        add_bar(db, session=dt.date(2023, 1, 4))  # bar after the signal's session

        result = runner.invoke(app, ["paper", "open", signal.signal_id])

        assert result.exit_code == 0, result.output
        assert "filled" in result.output
        assert "RESEARCH SIGNALS" in result.output  # disclaimer stays on every paper command

        portfolio = PaperPortfolio(cfg, db)
        open_trades = portfolio.open_trades()
        assert len(open_trades) == 1
        assert open_trades[0].symbol == "TEST"
        assert open_trades[0].shares == 10

    def test_open_unknown_signal_is_refused_cleanly(self, cli_env):
        result = runner.invoke(app, ["paper", "open", "does-not-exist"])

        assert result.exit_code == 1
        assert "no such signal" in result.output

    def test_open_reports_queued_when_no_bar_yet(self, cli_env):
        cfg, db = cli_env
        signal = make_signal()
        SignalLedger(db).record(signal)
        # No PriceBar stored after the signal's session at all.

        result = runner.invoke(app, ["paper", "open", signal.signal_id])

        assert result.exit_code == 1
        assert "queued" in result.output
        assert PaperPortfolio(cfg, db).open_trades() == []

    def test_open_rejected_when_price_never_reaches_entry_zone(self, cli_env):
        cfg, db = cli_env
        signal = make_signal(entry_low=50.0, entry_high=60.0)
        SignalLedger(db).record(signal)
        add_bar(db, session=dt.date(2023, 1, 4))  # low=99, never dips to 50-60

        result = runner.invoke(app, ["paper", "open", signal.signal_id])

        assert result.exit_code == 1
        assert "rejected" in result.output
        assert PaperPortfolio(cfg, db).open_trades() == []

    def test_open_rejected_when_db_kill_switch_engaged(self, cli_env):
        cfg, db = cli_env
        signal = make_signal()
        SignalLedger(db).record(signal)
        add_bar(db, session=dt.date(2023, 1, 4))
        PaperPortfolio(cfg, db).engage_kill_switch(True)

        result = runner.invoke(app, ["paper", "open", signal.signal_id])

        assert result.exit_code == 1
        assert "kill switch" in result.output.lower()
        assert PaperPortfolio(cfg, db).open_trades() == []

    def test_open_refused_when_config_kill_switch_engaged(self, cli_env, monkeypatch):
        _cfg, db = cli_env
        signal = make_signal()
        SignalLedger(db).record(signal)
        add_bar(db, session=dt.date(2023, 1, 4))
        monkeypatch.setenv("CLAUDETRADE_RISK__KILL_SWITCH_ENGAGED", "true")

        result = runner.invoke(app, ["paper", "open", signal.signal_id])

        assert result.exit_code == 1
        assert "refused" in result.output.lower()
        assert "kill switch" in result.output.lower()


class TestPaperProcess:
    def test_process_reports_no_open_positions(self, cli_env):
        result = runner.invoke(app, ["paper", "process"])

        assert result.exit_code == 0
        assert "no open paper positions" in result.output

    def test_process_closes_position_on_stop_hit(self, cli_env):
        cfg, db = cli_env
        signal = make_signal()
        SignalLedger(db).record(signal)
        add_bar(db, session=dt.date(2023, 1, 4))
        open_result = runner.invoke(app, ["paper", "open", signal.signal_id])
        assert open_result.exit_code == 0, open_result.output

        # A later bar whose low pierces the 95.0 stop.
        add_bar(db, session=dt.date(2023, 1, 5), open_=99.0, high=99.5, low=90.0, close=91.0)

        result = runner.invoke(app, ["paper", "process"])

        assert result.exit_code == 0, result.output
        assert "closed" in result.output
        assert PaperPortfolio(cfg, db).open_trades() == []

    def test_process_reports_missing_bar_without_crashing(self, cli_env):
        cfg, db = cli_env
        signal = make_signal(symbol="NOBAR")
        SignalLedger(db).record(signal)
        add_bar(db, symbol="NOBAR", session=dt.date(2023, 1, 4))
        open_result = runner.invoke(app, ["paper", "open", signal.signal_id])
        assert open_result.exit_code == 0, open_result.output

        # Simulate a symbol whose bar history was later purged entirely --
        # `process` must report it and move on, not crash.
        from sqlalchemy import delete

        with db.session() as db_session:
            db_session.execute(delete(PriceBar).where(PriceBar.symbol == "NOBAR"))

        result = runner.invoke(app, ["paper", "process"])

        assert result.exit_code == 0, result.output
        assert "no stored bar yet for: NOBAR" in result.output
        assert len(PaperPortfolio(cfg, db).open_trades()) == 1


class TestPaperClose:
    def test_close_unknown_trade_is_refused_cleanly(self, cli_env):
        result = runner.invoke(app, ["paper", "close", "trd-does-not-exist"])

        assert result.exit_code == 1
        assert "no such paper trade" in result.output

    def test_close_open_position_at_latest_price(self, cli_env):
        cfg, db = cli_env
        signal = make_signal()
        SignalLedger(db).record(signal)
        add_bar(db, session=dt.date(2023, 1, 4))
        runner.invoke(app, ["paper", "open", signal.signal_id])

        trade_id = PaperPortfolio(cfg, db).open_trades()[0].trade_id
        add_bar(db, session=dt.date(2023, 1, 6), open_=102.0, high=103.0, low=101.5, close=102.5)

        result = runner.invoke(app, ["paper", "close", trade_id])

        assert result.exit_code == 0, result.output
        assert "closed" in result.output
        assert PaperPortfolio(cfg, db).open_trades() == []
        closed = PaperPortfolio(cfg, db).closed_trades()
        assert len(closed) == 1
        assert closed[0].exit_reason == "manual"

    def test_close_already_closed_trade_is_refused_cleanly(self, cli_env):
        cfg, db = cli_env
        signal = make_signal()
        SignalLedger(db).record(signal)
        add_bar(db, session=dt.date(2023, 1, 4))
        runner.invoke(app, ["paper", "open", signal.signal_id])
        trade_id = PaperPortfolio(cfg, db).open_trades()[0].trade_id
        add_bar(db, session=dt.date(2023, 1, 6), open_=102.0, high=103.0, low=101.5, close=102.5)
        first = runner.invoke(app, ["paper", "close", trade_id])
        assert first.exit_code == 0, first.output

        second = runner.invoke(app, ["paper", "close", trade_id])

        assert second.exit_code == 1
        assert "closed" in second.output
        assert "cannot be closed again" in second.output

    def test_close_rejects_unknown_reason(self, cli_env):
        cfg, db = cli_env
        signal = make_signal()
        SignalLedger(db).record(signal)
        add_bar(db, session=dt.date(2023, 1, 4))
        runner.invoke(app, ["paper", "open", signal.signal_id])
        trade_id = PaperPortfolio(cfg, db).open_trades()[0].trade_id

        result = runner.invoke(app, ["paper", "close", trade_id, "--reason", "not-a-real-reason"])

        assert result.exit_code == 1
        assert "unknown exit reason" in result.output

    def test_close_with_no_stored_price_is_refused_cleanly(self, cli_env):
        cfg, db = cli_env
        signal = make_signal(symbol="NOPRICE")
        SignalLedger(db).record(signal)
        add_bar(db, symbol="NOPRICE", session=dt.date(2023, 1, 4))
        open_result = runner.invoke(app, ["paper", "open", signal.signal_id])
        assert open_result.exit_code == 0, open_result.output
        trade_id = PaperPortfolio(cfg, db).open_trades()[0].trade_id

        # Wipe stored bars so close_at_latest_price finds no price at all.
        from sqlalchemy import delete

        with db.session() as db_session:
            db_session.execute(delete(PriceBar).where(PriceBar.symbol == "NOPRICE"))

        result = runner.invoke(app, ["paper", "close", trade_id])

        assert result.exit_code == 1
        assert "no stored price" in result.output
