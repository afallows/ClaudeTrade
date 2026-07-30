"""Command-line interface.

Every workflow the application supports is reachable from here, so the UI is a
convenience rather than a requirement and the whole system is scriptable and
schedulable.

    claudetrade init                 # create the database and apply migrations
    claudetrade status               # provider health and data coverage
    claudetrade refresh              # pull data from configured providers
    claudetrade scan                 # generate ranked signals for a session
    claudetrade backtest             # replay strategies over history
    claudetrade paper ...            # inspect and drive the paper account
    claudetrade secrets ...          # store credentials in the OS keychain
    claudetrade db ...               # backup, restore, migrate
    claudetrade verify ...           # ledger integrity and reproducibility

Live trading is not implemented. ``--mode live`` is rejected.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from claudetrade.config import AppConfig, get_config
from claudetrade.logging_setup import setup_logging
from claudetrade.utils.timeutils import utc_now
from claudetrade.version import CODE_VERSION, DISCLAIMER, __version__

if TYPE_CHECKING:
    # Import cost avoided at module load (the backtest package pulls in
    # numpy/pandas); every command function already imports what it needs
    # locally. This is annotation-only, erased by `from __future__ import
    # annotations` above.
    from claudetrade.backtest.engine import BacktestResult

app = typer.Typer(
    name="claudetrade",
    help=f"Swing-trading research and decision support. {DISCLAIMER}",
    no_args_is_help=True,
    add_completion=False,
)
secrets_app = typer.Typer(help="Manage API credentials in the OS credential store.")
paper_app = typer.Typer(help="Inspect and drive the paper-trading account.")
db_app = typer.Typer(help="Database maintenance: migrate, backup, restore.")
verify_app = typer.Typer(help="Integrity and reproducibility checks.")
app.add_typer(secrets_app, name="secrets")
app.add_typer(paper_app, name="paper")
app.add_typer(db_app, name="db")
app.add_typer(verify_app, name="verify")

ConfigOption = Annotated[
    Path | None, typer.Option("--config", "-c", help="Path to config.toml.")
]


def _load(config_path: Path | None) -> AppConfig:
    config = get_config(config_path, reload=True)
    setup_logging(config)
    return config


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


def _today() -> dt.date:
    """Today's exchange-relevant date, derived from UTC.

    The application forbids naive datetimes everywhere else; the CLI's date
    defaults should not be the one exception.
    """
    return utc_now().date()


def _parse_date(value: str | None, default: dt.date) -> dt.date:
    if not value:
        return default
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"expected an ISO date (YYYY-MM-DD), got {value!r}") from None


def _load_verified_signal(db, signal_id: str):
    """Read one signal from the ledger with its integrity check intact.

    ``SignalLedger.get(..., verify=True)`` -- the default -- round-trips
    ``created_at`` through SQLite as a *naive* datetime even though every
    signal is written with ``ensure_utc()`` applied first (see
    ``SignalLedger.record``). Comparing the naive read-back against the hash
    computed over the tz-aware original then always fails, for every signal,
    regardless of tampering. Since the write path guarantees UTC, the fix
    belongs at the read boundary: re-attach UTC before verifying, which
    reproduces exactly the payload the hash was computed over. This does not
    touch ``claudetrade.signals.ledger`` (out of bounds for this change) --
    it is read-only usage of its public ``get(verify=False)`` and ``verify()``.
    """
    import dataclasses

    from claudetrade.db.models import SignalRow
    from claudetrade.signals.ledger import LedgerIntegrityError, SignalLedger

    ledger = SignalLedger(db)
    signal = ledger.get(signal_id, verify=False)
    if signal is None:
        return None
    if signal.created_at.tzinfo is None:
        signal = dataclasses.replace(signal, created_at=signal.created_at.replace(tzinfo=dt.UTC))

    with db.read_session() as session:
        row = session.get(SignalRow, signal_id)
    if row is not None and not ledger.verify(signal, row.integrity_hash):
        raise LedgerIntegrityError(
            f"signal {signal_id} failed its integrity check: the stored row does not match "
            "its recorded hash, which means it was modified outside the application"
        )
    return signal


def _render_funnel_report(result: BacktestResult) -> str:
    """ADR-0007 Decision 3(b): the rejection funnel, as markdown.

    ``backtest.reporting.render_markdown_report`` covers headline metrics and
    segments; the funnel is rendered here rather than there so a 0-trade run
    -- where the headline table is mostly zeros -- still has this section, and
    so it isn't lost if the metrics reconstruction in that module ever
    changes shape (``PerformanceMetrics(**result.metrics)``, unrelated to the
    funnel entirely).
    """
    lines = ["## Rejection Funnel\n"]
    if not result.trades:
        lines.append(
            "**0 completed trades.** The table below is why -- every "
            "candidate this run considered is accounted for by exactly one "
            "row.\n"
        )
    lines.append("```")
    lines.extend(result.funnel.summary_lines())
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Top-level commands
# --------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Show the version and code identity stamped onto generated artefacts."""
    typer.echo(f"claudetrade {__version__} (code_version={CODE_VERSION})")
    typer.echo(DISCLAIMER)


@app.command()
def init(config: ConfigOption = None) -> None:
    """Create the database, apply migrations and report where things live."""
    cfg = _load(config)
    from claudetrade.db.migrations import LATEST_VERSION, init_database
    from claudetrade.db.session import get_database

    db = get_database(cfg)
    applied = init_database(db)
    typer.echo(f"database:  {cfg.database_url()}")
    typer.echo(f"schema:    v{LATEST_VERSION} ({'applied ' + str(applied) if applied else 'already current'})")
    typer.echo(f"data dir:  {cfg.paths.resolve('data_dir')}")
    typer.echo(f"logs dir:  {cfg.paths.resolve('logs_dir')}")
    typer.echo(f"config hash: {cfg.config_hash[:16]}")

    packaged = ", ".join(cfg.universe.packaged_universes) or "none"
    typer.echo(
        f"\nuniverse:    source={cfg.universe.source}, packaged defaults={packaged} "
        f"(seed lists shipped with the app; used until a database of stored "
        f"securities exists, then merged with it -- see docs/api-providers.md)"
    )
    typer.echo(
        f"market data: provider={cfg.market_data.provider} "
        + (
            "(offline synthetic data -- zero API keys, zero network, the default)"
            if cfg.market_data.provider == "synthetic"
            else "(live)"
        )
    )
    if cfg.market_data.provider == "synthetic":
        typer.echo(
            "  to switch on real US + Canadian daily bars: set "
            'market_data.provider = "stooq" in config.toml, run `claudetrade probe` '
            "to confirm this machine can reach stooq.com, then `claudetrade refresh`. "
            "The default stays synthetic/offline; this is opt-in."
        )


@app.command()
def status(config: ConfigOption = None) -> None:
    """Provider health, enabled sources and stored data coverage."""
    cfg = _load(config)
    from sqlalchemy import func, select

    from claudetrade.db.models import PriceBar, Security, SignalRow, SocialPostRow
    from claudetrade.db.session import get_database
    from claudetrade.providers.registry import provider_status_report

    typer.echo(f"mode: {cfg.trading.mode}  (live trading is not implemented)")
    typer.echo("\nsources enabled:")
    for name, enabled in cfg.describe_enabled_sources().items():
        typer.echo(f"  {'on ' if enabled else 'off'}  {name}")

    typer.echo("\nproviders:")
    for report in provider_status_report(cfg):
        flag = "ok  " if report.available else "down"
        typer.echo(f"  {flag} {report.name:12s} {report.kind:8s} {report.message[:60]}")
        if report.licence_note:
            typer.echo(f"       licence: {report.licence_note[:80]}")

    db = get_database(cfg)
    with db.read_session() as session:
        counts = {
            "securities": session.execute(select(func.count()).select_from(Security)).scalar(),
            "price_bars": session.execute(select(func.count()).select_from(PriceBar)).scalar(),
            "social_posts": session.execute(
                select(func.count()).select_from(SocialPostRow)
            ).scalar(),
            "signals": session.execute(select(func.count()).select_from(SignalRow)).scalar(),
        }
    typer.echo("\nstored data:")
    for key, value in counts.items():
        typer.echo(f"  {key:14s} {value:,}")


#: Hosts each live source needs, with why and whether a credential is required.
#: Used by `claudetrade providers probe` so an operator can confirm egress and
#: credentials separately -- they fail in different ways and need different fixes.
LIVE_ENDPOINTS: tuple[tuple[str, str, str, bool], ...] = (
    ("market", "stooq.com", "https://stooq.com/q/d/l/?s=aapl.us&i=d", False),
    (
        "market",
        "query1.finance.yahoo.com",
        "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=5d&interval=1d",
        False,
    ),
    ("reddit", "www.reddit.com", "https://www.reddit.com/api/v1/access_token", True),
    ("reddit", "oauth.reddit.com", "https://oauth.reddit.com/api/v1/me", True),
    ("x", "api.x.com", "https://api.x.com/2/tweets/search/recent?query=test", True),
    ("ai", "api.anthropic.com", "https://api.anthropic.com/v1/models", True),
    ("ai", "api.openai.com", "https://api.openai.com/v1/models", True),
)


@app.command("probe")
def probe(
    config: ConfigOption = None,
    timeout: Annotated[float, typer.Option(help="Per-host timeout in seconds.")] = 15.0,
) -> None:
    """Test whether the live data endpoints are reachable from this machine.

    Distinguishes the two failure modes that look alike but need different
    fixes: a blocked network (the host cannot be reached at all, typically a
    proxy or firewall policy) and a missing credential (the host answers but
    rejects the request). Run this after changing an egress policy to confirm
    the change took effect.
    """
    cfg = _load(config)
    import httpx

    from claudetrade.secrets import has_secret

    typer.echo("Probing live data endpoints...\n")
    typer.echo(f"{'SOURCE':8s}{'HOST':34s}{'NETWORK':12s}{'CREDENTIAL':12s}NOTE")
    blocked: list[str] = []

    for source, host, url, needs_key in LIVE_ENDPOINTS:
        note = ""
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.get(url)
            # Any HTTP answer -- including 401/403 from the service itself --
            # proves the network path is open.
            network = "reachable"
            if response.status_code in (401, 403):
                note = f"HTTP {response.status_code}: needs credentials"
            else:
                note = f"HTTP {response.status_code}"
        except httpx.ProxyError as exc:
            network = "BLOCKED"
            note = f"proxy refused: {str(exc)[:48]}"
            blocked.append(host)
        except httpx.ConnectError as exc:
            network = "BLOCKED"
            note = f"connect failed: {str(exc)[:48]}"
            blocked.append(host)
        except Exception as exc:  # timeouts and anything else
            network = "BLOCKED"
            note = f"{type(exc).__name__}: {str(exc)[:40]}"
            blocked.append(host)

        if not needs_key:
            credential = "not needed"
        else:
            name = {
                "reddit": cfg.reddit.client_id_credential,
                "x": cfg.x.bearer_credential,
                "ai": cfg.ai.api_key_credential,
            }.get(source, "")
            credential = "configured" if (name and has_secret(name)) else "MISSING"

        typer.echo(f"{source:8s}{host:34s}{network:12s}{credential:12s}{note}")

    typer.echo("")
    if blocked:
        typer.secho(
            f"{len(blocked)} host(s) unreachable from this machine. If you are running inside a "
            "managed environment, these must be added to its egress allow-list by an "
            "administrator; the application cannot and must not work around that control.",
            fg=typer.colors.YELLOW,
        )
        typer.echo("Hosts to allow-list: " + ", ".join(sorted(set(blocked))))
    else:
        typer.secho("All probed hosts are reachable.", fg=typer.colors.GREEN)
    typer.echo(
        "\nCredentials are stored with:  claudetrade secrets set <name>\n"
        "Reddit needs a free script app (client id + secret); X search needs a paid tier."
    )


@app.command()
def refresh(
    config: ConfigOption = None,
    start: Annotated[str | None, typer.Option(help="ISO start date.")] = None,
    end: Annotated[str | None, typer.Option(help="ISO end date.")] = None,
    symbols: Annotated[str | None, typer.Option(help="Comma-separated symbols.")] = None,
) -> None:
    """Pull data from every configured provider and store it.

    With no ``--start``/``--end`` this covers the last 90 calendar days ending
    today -- enough recent history for the scan/backtest indicators without a
    new install's first real-data refresh pulling years of history for
    hundreds of symbols before showing anything.
    """
    cfg = _load(config)
    from claudetrade.pipeline import Pipeline

    end_date = _parse_date(end, _today())
    start_date = _parse_date(start, end_date - dt.timedelta(days=90))
    symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None

    pipeline = Pipeline.bootstrap(cfg)
    result = pipeline.refresh(start=start_date, end=end_date, symbols=symbol_list)
    _echo_json(
        {
            "summary": result.summary(),
            "ingest": result.ingest.summary() if result.ingest else None,
        }
    )
    if result.degraded_sources:
        typer.secho(
            "Some sources were unavailable; the run continued in reduced-capability mode.",
            fg=typer.colors.YELLOW,
        )


@app.command()
def scan(
    config: ConfigOption = None,
    session: Annotated[str | None, typer.Option(help="ISO session date.")] = None,
    lookback: Annotated[int, typer.Option(help="Days of history to load.")] = 500,
    record: Annotated[bool, typer.Option(help="Write signals to the ledger.")] = True,
    limit: Annotated[int, typer.Option(help="Rows to display.")] = 15,
) -> None:
    """Generate ranked swing-trade candidates for a session."""
    cfg = _load(config)
    from claudetrade.pipeline import Pipeline

    session_date = _parse_date(session, _today())
    pipeline = Pipeline.bootstrap(cfg)
    result = pipeline.scan(session_date, lookback_days=lookback, record=record)
    scan_result = result.scan
    if scan_result is None:
        typer.secho("scan produced no result", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.echo(f"\n{DISCLAIMER}\n")
    typer.echo(
        f"session {session_date} | regime {scan_result.regime.regime.value} | "
        f"{scan_result.evaluated_symbols} symbols evaluated"
    )
    if not scan_result.signals:
        typer.echo("\nNo candidate cleared the thresholds. An empty list is a valid result.")
    else:
        typer.echo(
            f"\n{'SYMBOL':8s}{'STRATEGY':24s}{'DIR':6s}{'SCORE':>7s}{'CONF':>7s}"
            f"{'R:R':>6s}{'SHARES':>8s}  STATUS"
        )
        for sig in scan_result.top(limit):
            typer.echo(
                f"{sig.symbol:8s}{sig.strategy:24s}{sig.direction.value:6s}"
                f"{sig.overall_score:7.1f}{sig.confidence:7.2f}"
                f"{sig.plan.reward_risk_ratio:6.2f}{sig.plan.shares:8d}  {sig.status.value}"
            )
    for warning in result.warnings:
        typer.secho(f"! {warning}", fg=typer.colors.YELLOW)


@app.command()
def backtest(
    config: ConfigOption = None,
    start: Annotated[str | None, typer.Option(help="ISO start date.")] = None,
    end: Annotated[str | None, typer.Option(help="ISO end date.")] = None,
    strategies: Annotated[str | None, typer.Option(help="Comma-separated strategy names.")] = None,
    report: Annotated[Path | None, typer.Option(help="Write a markdown report here.")] = None,
    export: Annotated[Path | None, typer.Option(help="Export trades/metrics CSV here.")] = None,
    walk_forward: Annotated[bool, typer.Option(help="Run walk-forward validation.")] = False,
) -> None:
    """Replay strategies over history and report performance."""
    cfg = _load(config)
    if strategies:
        cfg.signals.enabled_strategies = [s.strip() for s in strategies.split(",")]

    from claudetrade.backtest.engine import BacktestEngine
    from claudetrade.backtest.reporting import export_csv, render_markdown_report
    from claudetrade.pipeline import Pipeline

    end_date = _parse_date(end, _today())
    start_date = _parse_date(start, end_date - dt.timedelta(days=730))

    pipeline = Pipeline.bootstrap(cfg)
    universe = pipeline.universe.for_session(end_date)
    if not universe.symbols:
        typer.secho("universe is empty -- run 'claudetrade refresh' first", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.echo(f"building contexts for {len(universe.symbols)} symbols...")
    provider = pipeline.make_context_provider(
        symbols=universe.symbols, start=start_date, end=end_date
    )
    typer.echo(f"running backtest over {len(provider.sessions())} sessions...")
    engine = BacktestEngine(cfg)
    result = engine.run(provider, start_session=start_date, end_session=end_date)

    typer.echo(f"\n{DISCLAIMER}\n")
    typer.echo(render_markdown_report(result))

    # ADR-0007 Decision 3(b): rendered unconditionally, not only on a 0-trade
    # run -- a healthy run's funnel is what "healthy" looks like, and only
    # ever showing this table when something went wrong would make it look
    # like an error report instead of routine accounting.
    funnel_report = _render_funnel_report(result)
    typer.echo(funnel_report)
    if not result.trades:
        typer.secho(
            "0 completed trades -- see the Rejection Funnel above for why.",
            fg=typer.colors.YELLOW,
        )

    if walk_forward:
        typer.echo("\nwalk-forward validation is available via the backtest.walkforward module")

    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_markdown_report(result) + "\n" + funnel_report, encoding="utf-8")
        typer.echo(f"report written to {report}")
    if export:
        export.mkdir(parents=True, exist_ok=True)
        export_csv(result, export)
        typer.echo(f"CSV exported to {export}")


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------


@secrets_app.command("set")
def secrets_set(
    name: Annotated[str, typer.Argument(help="Credential name, e.g. anthropic_api_key.")],
) -> None:
    """Store a credential in the OS credential store.

    The value is read from a hidden prompt and never appears in shell history,
    the config file, or the logs.
    """
    from claudetrade.secrets import set_secret

    value = typer.prompt(f"value for {name}", hide_input=True)
    try:
        backend = set_secret(name, value)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.echo(f"stored '{name}' in the {backend} backend")


@secrets_app.command("list")
def secrets_list(config: ConfigOption = None) -> None:
    """Show which credentials resolve, without revealing any value."""
    cfg = _load(config)
    from claudetrade.secrets import describe_secrets

    names = [
        cfg.ai.api_key_credential,
        cfg.reddit.client_id_credential,
        cfg.reddit.client_secret_credential,
        cfg.x.bearer_credential,
        cfg.notifications.webhook_url_credential,
    ]
    if cfg.market_data.credential:
        names.append(cfg.market_data.credential)
    for name, info in describe_secrets(sorted(set(names))).items():
        typer.echo(f"  {name:28s} {info['configured']:4s} {info['source']:12s} {info['masked']}")


@secrets_app.command("delete")
def secrets_delete(name: str) -> None:
    """Remove a credential from the OS credential store."""
    from claudetrade.secrets import delete_secret

    typer.echo("removed" if delete_secret(name) else "nothing to remove")


# --------------------------------------------------------------------------
# paper
# --------------------------------------------------------------------------


@paper_app.command("status")
def paper_status(config: ConfigOption = None) -> None:
    """Paper account equity, open positions and performance."""
    cfg = _load(config)
    from claudetrade.db.session import get_database
    from claudetrade.paper.portfolio import PaperPortfolio

    portfolio = PaperPortfolio(cfg, get_database(cfg))
    account = portfolio.account()
    typer.echo(
        f"account '{account.name}': equity {account.equity:,.2f} cash {account.cash:,.2f} "
        f"realised {account.realised_pnl:,.2f}"
        + ("  [KILL SWITCH ENGAGED]" if account.kill_switch_engaged else "")
    )
    performance = portfolio.performance()
    warnings = performance.pop("warnings", [])
    _echo_json(performance)
    for warning in warnings:
        typer.secho(f"! {warning}", fg=typer.colors.YELLOW)


@paper_app.command("positions")
def paper_positions(config: ConfigOption = None) -> None:
    """Open paper positions and any that need attention."""
    cfg = _load(config)
    from claudetrade.db.session import get_database
    from claudetrade.paper.portfolio import PaperPortfolio

    portfolio = PaperPortfolio(cfg, get_database(cfg))
    views = portfolio.positions({})
    if not views:
        typer.echo("no open paper positions")
        return
    for view in views:
        notes = view.needs_attention()
        typer.echo(
            f"{view.symbol:8s} {view.direction.value:6s} {view.shares:6d} @ "
            f"{view.entry_price:8.2f} stop {view.stop_loss:8.2f} "
            f"held {view.days_held}d"
            + (f"  <- {'; '.join(notes)}" if notes else "")
        )


@paper_app.command("open")
def paper_open(
    signal_id: Annotated[str, typer.Argument(help="Signal id from the ledger (see `claudetrade scan`).")],
    config: ConfigOption = None,
) -> None:
    """Submit a recorded signal to the paper broker for execution.

    Looks the signal up in the immutable ledger (read-only) and submits it
    through ``PaperBroker.submit_order`` -- the ``BrokerProvider`` seam -- so
    the kill-switch/mode guard runs before anything else does. Execution is
    priced on the first stored bar after the signal's own session, matching
    ``PaperBroker.submit_signal``'s look-ahead rule; if that bar has not been
    ingested yet, the order is reported as not fillable rather than faked.
    """
    cfg = _load(config)
    from claudetrade.brokers.base import OrderRequest, TradingHaltedError
    from claudetrade.db.session import get_database
    from claudetrade.paper.broker import PaperBroker
    from claudetrade.signals.ledger import LedgerIntegrityError

    db = get_database(cfg)
    try:
        signal = _load_verified_signal(db, signal_id)
    except LedgerIntegrityError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    if signal is None:
        typer.secho(f"no such signal: {signal_id}", fg=typer.colors.RED)
        raise typer.Exit(1)

    broker = PaperBroker(cfg, db)
    next_bar = broker.next_bar_after(signal.symbol, signal.session)
    if next_bar is None:
        typer.secho(
            f"queued: no stored bar for {signal.symbol} after {signal.session} yet -- "
            "run 'claudetrade refresh' and retry",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)

    request = OrderRequest(signal=signal, next_bar=next_bar, marks=broker.marks_for_open_positions())
    try:
        order = broker.submit_order(request)
    except TradingHaltedError as exc:
        typer.secho(f"refused: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    typer.echo(f"\n{DISCLAIMER}\n")
    if order.status.value == "filled":
        typer.secho(
            f"filled: {order.symbol} {order.filled_shares} shares @ "
            f"{order.average_fill_price:.2f} on {next_bar.session} (order {order.order_id})",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(f"rejected: {'; '.join(order.reasons) or 'no reason given'}", fg=typer.colors.RED)
        raise typer.Exit(1)


@paper_app.command("process")
def paper_process(config: ConfigOption = None) -> None:
    """Advance every open paper position against the latest stored bars.

    Applies stops, targets and time stops the same way the backtester does
    (``PaperBroker.process_open_positions``), then marks the account to
    market. A symbol with no stored bar yet is reported and left untouched
    rather than silently skipped.
    """
    cfg = _load(config)
    from claudetrade.db.session import get_database
    from claudetrade.paper.broker import PaperBroker

    db = get_database(cfg)
    broker = PaperBroker(cfg, db)
    open_symbols = {row.symbol for row in broker.portfolio.open_trades()}
    if not open_symbols:
        typer.echo("no open paper positions to process")
        return

    bars = broker.latest_bars_for_open_positions()
    missing = sorted(open_symbols - bars.keys())
    if missing:
        typer.secho(
            f"no stored bar yet for: {', '.join(missing)} -- run 'claudetrade refresh'",
            fg=typer.colors.YELLOW,
        )
    if not bars:
        typer.echo("no bars available to process against")
        return

    session_date = max(bar.session for bar in bars.values())
    results = broker.process_open_positions(bars, session_date)
    if not results:
        typer.echo(f"processed against {session_date}: no exits triggered")
        return
    typer.echo(f"processed against {session_date}:")
    for result in results:
        reason = result.reasons[0] if result.reasons else "closed"
        typer.echo(f"  closed {result.symbol:8s} {result.shares:6d} @ {result.fill_price:8.2f}  ({reason})")


@paper_app.command("close")
def paper_close(
    trade_id: Annotated[str, typer.Argument(help="Paper trade id to close (see `claudetrade paper positions`).")],
    reason: Annotated[str, typer.Option(help="Exit reason recorded against the trade.")] = "manual",
    config: ConfigOption = None,
) -> None:
    """Close an open paper position at the latest available price.

    Uses the same close/costing path as an automatic stop or target exit
    (``PaperBroker.close_at_latest_price`` -> ``PaperPortfolio.close_trade``).
    Refuses cleanly for an unknown or already-closed trade id.
    """
    cfg = _load(config)
    from claudetrade.brokers.base import BrokerOrderError
    from claudetrade.db.session import get_database
    from claudetrade.domain import ExitReason
    from claudetrade.paper.broker import PaperBroker

    try:
        exit_reason = ExitReason(reason)
    except ValueError:
        valid = ", ".join(r.value for r in ExitReason)
        typer.secho(f"unknown exit reason {reason!r}; choose from: {valid}", fg=typer.colors.RED)
        raise typer.Exit(1) from None

    db = get_database(cfg)
    broker = PaperBroker(cfg, db)
    try:
        trade = broker.close_at_latest_price(trade_id, reason=exit_reason)
    except BrokerOrderError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    typer.secho(
        f"closed {trade.trade_id}: {trade.symbol} {trade.shares} shares @ "
        f"{trade.exit_price:.2f} on {trade.exit_session} ({trade.exit_reason.value}) "
        f"net P&L {trade.net_pnl:,.2f}",
        fg=typer.colors.GREEN,
    )


@paper_app.command("kill-switch")
def paper_kill_switch(
    engage: Annotated[bool, typer.Option("--engage/--release")] = True,
    config: ConfigOption = None,
) -> None:
    """Engage or release the emergency kill switch.

    Engaging blocks all new entries. It deliberately does not liquidate: that
    is the operator's decision, not an automated one.
    """
    cfg = _load(config)
    from claudetrade.db.session import get_database
    from claudetrade.paper.portfolio import PaperPortfolio

    portfolio = PaperPortfolio(cfg, get_database(cfg))
    portfolio.engage_kill_switch(engage)
    typer.secho(
        "kill switch ENGAGED: no new entries will be accepted"
        if engage
        else "kill switch released",
        fg=typer.colors.RED if engage else typer.colors.GREEN,
    )


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------


@db_app.command("migrate")
def db_migrate(config: ConfigOption = None) -> None:
    """Apply any outstanding schema migrations."""
    cfg = _load(config)
    from claudetrade.db.migrations import current_version, migrate
    from claudetrade.db.session import get_database

    db = get_database(cfg)
    applied = migrate(db)
    typer.echo(f"schema now at v{current_version(db)}; applied {applied or 'nothing'}")


@db_app.command("backup")
def db_backup(config: ConfigOption = None, label: str = "") -> None:
    """Write a consistent snapshot of the database."""
    cfg = _load(config)
    from claudetrade.db.backup import create_backup
    from claudetrade.db.session import get_database

    path = create_backup(get_database(cfg), cfg.paths.resolve("backups_dir"), label=label)
    typer.echo(f"backup written to {path}")


@db_app.command("restore")
def db_restore(
    backup: Annotated[Path, typer.Argument(help="Backup file to restore.")],
    config: ConfigOption = None,
    force: Annotated[bool, typer.Option(help="Replace an existing database.")] = False,
) -> None:
    """Restore the database from a backup."""
    cfg = _load(config)
    from claudetrade.db.backup import restore_backup

    try:
        target = restore_backup(backup, cfg.database_url(), force=force)
    except FileExistsError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.echo(f"restored to {target}")


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


@verify_app.command("ledger")
def verify_ledger(config: ConfigOption = None) -> None:
    """Check every stored signal against its integrity hash."""
    cfg = _load(config)
    from claudetrade.db.session import get_database
    from claudetrade.signals.ledger import SignalLedger

    failures = SignalLedger(get_database(cfg)).verify_all()
    if failures:
        typer.secho(
            f"{len(failures)} signals failed their integrity check: {failures[:5]}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    typer.secho("all signals verified: none have been modified since they were written", fg=typer.colors.GREEN)


@verify_app.command("survivorship")
def verify_survivorship(
    config: ConfigOption = None,
    start: Annotated[str | None, typer.Option(help="ISO start date.")] = None,
    end: Annotated[str | None, typer.Option(help="ISO end date.")] = None,
) -> None:
    """Report whether the universe contains delisted companies.

    A multi-year universe with no delistings is survivorship-biased, and any
    backtest over it overstates achievable performance.
    """
    cfg = _load(config)
    from claudetrade.data.universe import UniverseSelector
    from claudetrade.db.session import get_database

    end_date = _parse_date(end, _today())
    start_date = _parse_date(start, end_date - dt.timedelta(days=1095))
    selector = UniverseSelector(cfg, get_database(cfg))
    _echo_json(selector.survivorship_check(start_date, end_date))


@app.command()
def ui(config: ConfigOption = None, port: int | None = None) -> None:
    """Launch the desktop interface (Streamlit)."""
    cfg = _load(config)
    from claudetrade import ui as ui_pkg

    app_path = Path(ui_pkg.__file__).parent / "app.py"
    resolved_port = port or cfg.ui.port
    typer.echo(f"starting the interface on port {resolved_port} ...")

    if getattr(sys, "frozen", False):
        # Under a PyInstaller-frozen build, `sys.executable` is the bootloader
        # binary (this very program), not a Python interpreter -- spawning
        # ``[sys.executable, "-m", "streamlit", ...]`` would try to re-exec the
        # frozen claudetrade.exe as if it were `python -m streamlit`, which
        # fails. Streamlit's own bootstrap API runs the server in this same
        # process instead, sidestepping subprocess entirely.
        _run_streamlit_in_process(str(app_path), resolved_port)
        return

    import subprocess

    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(resolved_port),
    ]
    raise typer.Exit(subprocess.call(command))


def _run_streamlit_in_process(app_path: str, port: int) -> None:
    """Launch Streamlit without spawning a subprocess (the frozen-exe path).

    Kept tiny and isolated so a unit test can monkeypatch ``sys.frozen`` and
    assert *this* is the branch ``ui()`` chooses, by mocking
    ``streamlit.web.bootstrap.run`` rather than actually starting a server.
    """
    from streamlit.web import bootstrap

    bootstrap.run(app_path, False, [], {"server.port": port})


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
