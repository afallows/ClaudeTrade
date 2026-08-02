"""Command-line interface.

Every workflow the application supports is reachable from here, so the UI is a
convenience rather than a requirement and the whole system is scriptable and
schedulable.

    claudetrade init                 # create the database and apply migrations
    claudetrade status               # provider health and data coverage
    claudetrade refresh              # pull data from configured providers
    claudetrade scan                 # generate ranked signals for a session
    claudetrade backtest             # replay strategies over history
    claudetrade backtest report      # honest, per-strategy walk-forward evidence report
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
from claudetrade.utils.timeutils import current_trading_session, utc_now
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
sentiment_app = typer.Typer(help="Sentiment and mention history: per-symbol series, what is rising.")
verify_app = typer.Typer(help="Integrity and reproducibility checks.")
#: A Typer group rather than a plain command so ``claudetrade backtest report``
#: (the multi-strategy owner report, see ``backtest.report``) can live
#: alongside the original single-run ``claudetrade backtest`` -- which keeps
#: working unchanged as this group's callback, invoked when no subcommand is
#: given (see ``backtest()`` below).
backtest_app = typer.Typer(help="Replay strategies over history and report performance.")
app.add_typer(secrets_app, name="secrets")
app.add_typer(paper_app, name="paper")
app.add_typer(db_app, name="db")
app.add_typer(sentiment_app, name="sentiment")
app.add_typer(verify_app, name="verify")
app.add_typer(backtest_app, name="backtest")

ConfigOption = Annotated[
    Path | None, typer.Option("--config", "-c", help="Path to config.toml.")
]


def _load(config_path: Path | None) -> AppConfig:
    config = get_config(config_path, reload=True)
    setup_logging(config, component="cli")
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


def _parse_optional_date(value: str | None) -> dt.date | None:
    """Like ``_parse_date`` but with no default -- ``None`` means 'let the caller decide'.

    Used by ``backtest report``, where an unset ``--start``/``--end`` means
    "everything available" rather than a fixed lookback window.
    """
    if not value:
        return None
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
            "(offline synthetic demo data -- fabricated tickers)"
            if cfg.market_data.provider == "synthetic"
            else "(live; TipRanks is the default, with a Yahoo bar fallback)"
        )
    )
    if cfg.market_data.provider == "synthetic":
        typer.echo(
            "  WARNING: synthetic mode creates fabricated tickers. To restore real "
            "US + Canadian daily bars, market caps and earnings, set "
            'market_data.provider = "tipranks" in config.toml, run `claudetrade probe` '
            "to confirm this machine can reach widgets.tipranks.com, then `claudetrade refresh`."
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
    # TipRanks widget API (primary market-data/earnings source; ADR-0008
    # Decision 1 amendment). Unauthenticated and keyless, so needs_key=False
    # is fully accurate -- see providers.market.tipranks and
    # docs/api-providers.md for the ToS posture.
    (
        "market",
        "widgets.tipranks.com",
        "https://widgets.tipranks.com/api/etoro/dataForTicker?ticker=AAPL",
        False,
    ),
    ("reddit", "www.reddit.com", "https://www.reddit.com/api/v1/access_token", True),
    ("reddit", "oauth.reddit.com", "https://oauth.reddit.com/api/v1/me", True),
    # Public JSON fallback (ADR-0008 Decision 1): genuinely credential-free,
    # so needs_key=False here is fully accurate, unlike the x_session row below.
    (
        "reddit",
        "www.reddit.com",
        "https://www.reddit.com/r/stocks/new.json?limit=1",
        False,
    ),
    ("x", "api.x.com", "https://api.x.com/2/tweets/search/recent?query=test", True),
    # X cookie-session mode (ADR-0008 Decision 1 / Decision 5). Real reachability
    # check only -- the CREDENTIAL column below cannot reflect this row
    # accurately: it is wired to look up cfg.x.bearer_credential (the official
    # API token) keyed by the "x" source name, and extending that lookup to
    # also check x_auth_token/x_ct0 is out of scope for this change (probe's
    # credential-name mapping lives further down in this function, which is
    # outside the host-list-only boundary this change was scoped to). Using a
    # distinct, unmapped source name here means the CREDENTIAL column always
    # reads "MISSING" for this row regardless of whether the session cookies
    # are actually configured -- an honest "not wired up yet" placeholder
    # rather than a false "configured"/"not needed". Verify session cookies
    # separately with `claudetrade secrets list`.
    ("x_session", "x.com", "https://x.com/i/api/graphql/placeholder/SearchTimeline", True),
    # Stocktwits public symbol-stream API (ADR-0008 Decision 1): keyless by
    # design, so needs_key=False is fully accurate.
    (
        "stocktwits",
        "api.stocktwits.com",
        "https://api.stocktwits.com/api/2/streams/symbol/AAPL.json",
        False,
    ),
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
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help="Re-fetch every symbol even if its stored bars are already current.",
        ),
    ] = False,
) -> None:
    """Pull data from every configured provider and store it.

    With no ``--start``/``--end`` this covers the last 90 calendar days ending
    today -- enough recent history for the scan/backtest indicators without a
    new install's first real-data refresh pulling years of history for
    hundreds of symbols before showing anything.

    By default only symbols whose stored bars are BEHIND the window's last
    trading session are fetched. On a per-symbol provider every fetch is
    rate-limited individually (120/min), so re-requesting an already-current
    2,400-symbol universe costs ~20 minutes and changes nothing. ``--full``
    forces the complete sweep (use it to repair interior gaps, though
    ``claudetrade db backfill`` is the purpose-built tool for that).
    """
    cfg = _load(config)
    if full:
        cfg.market_data.incremental_prices = False
    from claudetrade.db import refresh_state_store
    from claudetrade.pipeline import Pipeline

    end_date = _parse_date(end, _today())
    start_date = _parse_date(start, end_date - dt.timedelta(days=90))
    symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None

    pipeline = Pipeline.bootstrap(cfg)
    # Cross-process single-flight (QA handoff v3, F27): the web API and MCP
    # server write the same database file, and two concurrent refreshes race
    # each other's writes. The lock lives in the database, heartbeats through
    # the ordinary progress callback, and a holder that died mid-run is taken
    # over automatically -- so a crash here never wedges future refreshes.
    outcome = refresh_state_store.try_acquire(pipeline.db, "cli")
    if not outcome.acquired:
        holder = outcome.holder
        typer.secho(
            "Refusing to start: "
            + (holder.describe() if holder else "another process holds the refresh lock")
            + ". Wait for it to finish (or, if its process died, retry once its "
            "lock goes stale -- about two minutes).",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)
    handle = outcome.handle
    try:
        result = pipeline.refresh(
            start=start_date,
            end=end_date,
            symbols=symbol_list,
            progress_callback=handle.update_progress,
        )
    except Exception as exc:
        handle.finish("failed", error=str(exc))
        raise
    else:
        handle.finish("done")
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

    # Scan defaults to the ET trading session, not the UTC calendar date --
    # a Friday-evening scan must request Friday, not Saturday.
    session_date = _parse_date(session, current_trading_session())
    pipeline = Pipeline.bootstrap(cfg)
    result = pipeline.scan(session_date, lookback_days=lookback, record=record)
    scan_result = result.scan
    if scan_result is None:
        for warning in result.warnings:
            typer.secho(warning, fg=typer.colors.YELLOW)
        typer.secho("scan produced no result", fg=typer.colors.RED)
        raise typer.Exit(1)

    for warning in result.warnings:
        typer.secho(warning, fg=typer.colors.YELLOW)
    typer.echo(f"\n{DISCLAIMER}\n")
    typer.echo(
        # scan_result.session, not session_date: the pipeline may have fallen
        # back to the latest stored session (the warnings above explain).
        f"session {scan_result.session} | regime {scan_result.regime.regime.value} | "
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


@backtest_app.callback(invoke_without_command=True)
def backtest(
    ctx: typer.Context,
    config: ConfigOption = None,
    start: Annotated[str | None, typer.Option(help="ISO start date.")] = None,
    end: Annotated[str | None, typer.Option(help="ISO end date.")] = None,
    strategies: Annotated[str | None, typer.Option(help="Comma-separated strategy names.")] = None,
    report: Annotated[Path | None, typer.Option(help="Write a markdown report here.")] = None,
    export: Annotated[Path | None, typer.Option(help="Export trades/metrics CSV here.")] = None,
    walk_forward: Annotated[bool, typer.Option(help="Run walk-forward validation.")] = False,
) -> None:
    """Replay strategies over history and report performance.

    Bare ``claudetrade backtest`` runs one combined-strategy backtest exactly
    as before. For the honest, per-strategy, significance-gated evidence
    report an owner would hand to themselves before trusting a
    recommendation, see ``claudetrade backtest report``.
    """
    if ctx.invoked_subcommand is not None:
        # A subcommand (e.g. `report`) was given -- this group callback still
        # runs first (that's how Typer/Click groups work), but the single-run
        # backtest below must not also execute.
        return

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


@backtest_app.command("report")
def backtest_report_cmd(
    config: ConfigOption = None,
    start: Annotated[
        str | None, typer.Option(help="ISO start date (default: earliest stored session).")
    ] = None,
    end: Annotated[
        str | None, typer.Option(help="ISO end date (default: latest stored session).")
    ] = None,
    strategies: Annotated[
        str | None,
        typer.Option(help="Comma-separated strategy names (default: every registered strategy)."),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(help="Directory for the .md/.json report (default: the exports directory)."),
    ] = None,
) -> None:
    """Generate the honest, multi-strategy backtest REPORT (Markdown + JSON).

    Runs walk-forward validation for every registered strategy, in isolation,
    over the bars/sentiment/earnings already stored in this installation's
    database (the full available history by default), then writes ONE
    Markdown report and its JSON twin to ``--output-dir`` (default: the
    configured exports directory, ``backtest-report-<date>.{md,json}``).

    Every headline is significance-gated: a strategy without enough
    out-of-sample evidence is reported as 'INSUFFICIENT EVIDENCE', not its
    best-looking point estimate, and a strategy with zero trades in the
    window says so plainly instead of rendering a table of zeros. See
    docs/backtest-report.md.
    """
    cfg = _load(config)
    from claudetrade.backtest.report import (
        generate_backtest_report,
        render_report_markdown,
        save_report,
    )
    from claudetrade.pipeline import Pipeline

    start_date = _parse_optional_date(start)
    end_date = _parse_optional_date(end)
    strategy_list = [s.strip() for s in strategies.split(",")] if strategies else None

    pipeline = Pipeline.bootstrap(cfg)
    typer.echo(
        "running walk-forward backtests for every strategy, in isolation -- this replays "
        "the full signal engine per strategy and can take a while on a large universe/window..."
    )
    try:
        report_obj = generate_backtest_report(
            pipeline, cfg, start=start_date, end=end_date, strategy_names=strategy_list
        )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from None

    out_dir = output_dir or cfg.paths.resolve("exports_dir")
    md_path, json_path = save_report(report_obj, out_dir)

    typer.echo(f"\n{DISCLAIMER}\n")
    typer.echo(render_report_markdown(report_obj))
    typer.echo(f"\nmarkdown report: {md_path}")
    typer.echo(f"json report:     {json_path}")


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


@db_app.command("purge-synthetic")
def db_purge_synthetic(config: ConfigOption = None) -> None:
    """Delete social posts/mentions/sentiment aggregates that came from the
    offline synthetic generator.

    ``reddit.provider`` (and, less commonly, ``x.provider``/``news.provider``)
    used to default to ``"synthetic"``; an install left on that default while
    the operator believed it was live would silently fill the database with
    fabricated posts -- a real refresh log showed tens of thousands of them.
    Changing the default going forward does not clean up a database that
    already has them, so this exists as a scoped fix short of a full reset.

    Synthetic posts are identified by their ``external_id`` prefix
    (``"synthetic-"``), stamped unconditionally by every synthetic social
    adapter (``providers.social.synthetic.SyntheticSocialProvider``,
    regardless of which ``SocialSource`` -- reddit/x/news -- it is filed
    under) -- no live adapter ever produces an id in that form.

    Daily sentiment aggregates (``symbol_sentiment_daily``) cannot be
    attributed to a single originating post without a recompute, so ALL of
    them are cleared too, not just the ones touching a purged post -- run
    `claudetrade refresh` afterwards to rebuild them from whichever sources
    are configured now.
    """
    cfg = _load(config)
    from sqlalchemy import select

    from claudetrade.db.models import (
        SentimentRecordRow,
        SocialPostRow,
        SymbolSentimentDaily,
        TickerMentionRow,
    )
    from claudetrade.db.session import get_database

    db = get_database(cfg)
    with db.session() as session:
        synthetic_posts = (
            session.execute(
                select(SocialPostRow).where(SocialPostRow.external_id.like("synthetic-%"))
            )
            .scalars()
            .all()
        )
        post_ids = [p.id for p in synthetic_posts]

        mentions_deleted = 0
        if post_ids:
            mentions = (
                session.execute(
                    select(TickerMentionRow).where(TickerMentionRow.post_id.in_(post_ids))
                )
                .scalars()
                .all()
            )
            mentions_deleted = len(mentions)
            for mention in mentions:
                session.delete(mention)

            # A second foreign key into social_posts -- the per-post,
            # per-symbol classifier output -- must also go before the post
            # itself can be deleted.
            sentiment_records = (
                session.execute(
                    select(SentimentRecordRow).where(SentimentRecordRow.post_id.in_(post_ids))
                )
                .scalars()
                .all()
            )
            for record in sentiment_records:
                session.delete(record)

        posts_deleted = len(synthetic_posts)
        for post in synthetic_posts:
            session.delete(post)

        aggregates = session.execute(select(SymbolSentimentDaily)).scalars().all()
        aggregates_deleted = len(aggregates)
        for aggregate in aggregates:
            session.delete(aggregate)

    _echo_json(
        {
            "posts_deleted": posts_deleted,
            "mentions_deleted": mentions_deleted,
            "sentiment_aggregates_deleted": aggregates_deleted,
        }
    )
    if posts_deleted == 0:
        typer.secho("No synthetic-origin posts found -- nothing to purge.", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"Purged {posts_deleted} synthetic post(s), {mentions_deleted} mention(s), and "
            f"{aggregates_deleted} sentiment aggregate row(s). Run 'claudetrade refresh' to "
            "rebuild aggregates from your currently configured (live) sources.",
            fg=typer.colors.YELLOW,
        )


@db_app.command("rebuild-sentiment")
def db_rebuild_sentiment(
    config: ConfigOption = None,
    days: int = typer.Option(
        90, help="Rebuild sentiment for sessions this many days back from today."
    ),
) -> None:
    """Recompute daily sentiment aggregates from the posts already stored in
    the database, using the CURRENT entity-resolution and classifier code.

    Exists because stored aggregates outlive the bugs that produced them:
    the trending list kept surfacing common-word "tickers" (AS, YOU, DAY --
    all genuine symbols, resolved from ordinary English by a since-fixed
    extractor) long after the extractor was fixed, because ``get_trending``
    reads ``symbol_sentiment_daily`` rows that nothing ever revisited. This
    clears every stored mention and aggregate row and rebuilds the aggregates
    from the sanitised posts on disk, so one command brings the stored view
    in line with the current code. Ticker-mention provenance rows repopulate
    for newly fetched posts on subsequent refreshes.

    Thin wrapper over ``sentiment.rebuild.rebuild_sentiment`` -- the same
    core the bootstrap self-heal runs automatically when the stored
    extraction version falls behind ``sentiment.EXTRACTION_VERSION``, so an
    operator who never reads this help text is healed anyway; this command
    remains for explicit/manual runs (e.g. a wider ``--days`` window).
    """
    cfg = _load(config)
    from claudetrade.db.session import get_database
    from claudetrade.sentiment.rebuild import RebuildUnavailableError, rebuild_sentiment

    db = get_database(cfg)
    try:
        summary = rebuild_sentiment(cfg, db, days=days)
    except RebuildUnavailableError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    _echo_json(
        {
            "posts_considered": summary["posts_considered"],
            "mentions_deleted": summary["mentions_deleted"],
            "sentiment_aggregates_deleted": summary["sentiment_aggregates_deleted"],
            "sentiment_rows_rebuilt": summary["sentiment_rows_rebuilt"],
            "symbols_affected": summary["symbols_affected"],
        }
    )
    typer.secho(
        f"Rebuilt {summary['sentiment_rows_rebuilt']} sentiment row(s) from "
        f"{summary['posts_considered']} stored post(s) "
        f"(cleared {summary['sentiment_aggregates_deleted']} stale aggregate(s), "
        f"{summary['mentions_deleted']} stale mention(s)).",
        fg=typer.colors.GREEN,
    )


def _backfill_via_market_chain(
    cfg: AppConfig,
    db,
    targets: list[str],
    start_date: dt.date,
    end_date: dt.date,
    *,
    force: bool,
) -> dict[str, int]:
    """Keyless historical backfill through the configured provider cascade.

    The grouped-daily path below is faster per call, but it needs a Polygon
    key, and requiring a signup to escape ``insufficient_history`` would put
    the fix to this application's most serious defect behind a paywall-shaped
    door. It does not need to be: Yahoo's chart endpoint (already in the
    default ``market_data.fallbacks``) returns *years* of daily bars for a
    symbol in a single call, and ``get_daily_bars`` already fans symbols out
    across ``MarketDataConfig.max_workers``. One call per symbol for the whole
    window is a completely reasonable one-time cost -- and unlike the grouped
    path it also covers TSX names, which Polygon's ``locale=us`` grouped
    endpoint never returns.

    Symbol-keyed rather than date-keyed, which inverts the resume behaviour:
    progress is committed per symbol chunk, so an interrupted run leaves
    whole symbols complete rather than whole dates. Re-running skips sessions
    already stored for that symbol.
    """
    from sqlalchemy import select

    from claudetrade.db.models import PriceBar
    from claudetrade.providers.base import ProviderError
    from claudetrade.providers.registry import get_market_provider

    market = get_market_provider(cfg)
    chunk_size = max(1, min(cfg.market_data.max_symbols_per_request, 100))
    stats = {"bars_written": 0, "symbols_covered": 0, "symbols_failed": 0}

    typer.echo(
        f"no Polygon key configured -- backfilling through the market provider chain "
        f"({getattr(market, 'name', 'market')}: "
        f"{cfg.market_data.provider} -> {', '.join(cfg.market_data.fallbacks)}), "
        f"one call per symbol for the whole window, "
        f"{cfg.market_data.max_workers} in parallel"
    )

    chunks = [targets[i : i + chunk_size] for i in range(0, len(targets), chunk_size)]
    for index, chunk in enumerate(chunks, start=1):
        try:
            fetched = market.get_daily_bars(chunk, start_date, end_date)
        except ProviderError as exc:
            stats["symbols_failed"] += len(chunk)
            typer.secho(f"  chunk {index}/{len(chunks)} failed: {exc}", fg=typer.colors.YELLOW)
            continue

        # One short transaction per chunk -- never a long one, matching the
        # write-scope posture the rest of this codebase now holds.
        with db.session() as session:
            for symbol, bars in fetched.items():
                if not bars:
                    stats["symbols_failed"] += 1
                    continue
                existing = {
                    row.session: row
                    for row in session.execute(
                        select(PriceBar).where(
                            PriceBar.symbol == symbol,
                            PriceBar.session >= start_date,
                            PriceBar.session <= end_date,
                        )
                    ).scalars()
                }
                wrote = 0
                for bar in bars:
                    row = existing.get(bar.session)
                    if row is not None and not force:
                        continue  # already stored; resume cheaply
                    if row is not None:
                        row.open, row.high, row.low, row.close = (
                            bar.open, bar.high, bar.low, bar.close,
                        )
                        row.adj_close = bar.adj_close
                        row.volume = bar.volume
                        row.source = bar.source or "backfill"
                        row.ingested_at = utc_now()
                    else:
                        session.add(
                            PriceBar(
                                symbol=symbol,
                                session=bar.session,
                                open=bar.open,
                                high=bar.high,
                                low=bar.low,
                                close=bar.close,
                                adj_close=bar.adj_close,
                                volume=bar.volume,
                                source=bar.source or "backfill",
                            )
                        )
                    wrote += 1
                if wrote:
                    stats["bars_written"] += wrote
                    stats["symbols_covered"] += 1

        typer.echo(
            f"  {index}/{len(chunks)} chunks | {stats['symbols_covered']} symbols | "
            f"{stats['bars_written']} bars written"
        )
    return stats


@db_app.command("backfill")
def db_backfill(
    config: ConfigOption = None,
    years: Annotated[
        int, typer.Option(help="Years of history to backfill (ignored when --start is given).")
    ] = 2,
    start: Annotated[str | None, typer.Option(help="ISO start date (overrides --years).")] = None,
    end: Annotated[
        str | None, typer.Option(help="ISO end date; defaults to the current trading session.")
    ] = None,
    force: Annotated[
        bool, typer.Option(help="Re-fetch and replace dates that already have stored bars.")
    ] = False,
    only_if_missing: Annotated[
        bool,
        typer.Option(
            help="Exit quietly (status 0) when enough history is already stored. "
            "For unattended callers like the setup script."
        ),
    ] = False,
    min_sessions: Annotated[
        int,
        typer.Option(
            help="With --only-if-missing, how many stored sessions count as 'enough'."
        ),
    ] = 150,
) -> None:
    """One-time historical price backfill. No API key required.

    QA handoff v3 F23: the scanner gates on 60+ sessions of price history, but
    a fresh install's refresh only ever adds ~1 real session per day -- so
    without a backfill the scanner rejects the whole universe with
    ``insufficient_history`` for weeks.

    Two paths, chosen automatically:

    * **No Polygon key (the default).** Fetches each symbol's full window
      through the configured market-provider chain -- Yahoo's chart endpoint
      returns years of daily bars per symbol in one call, and symbols fan out
      across ``max_workers`` in parallel. Keyless, and it covers TSX names
      too. This is the path the setup script uses.
    * **Polygon key present.** Walks trading dates NEWEST -> OLDEST (the
      scanner becomes useful as soon as the most recent ~60 sessions land),
      fetching each date's ENTIRE US market in one grouped call. Fewer calls,
      but free-tier pacing (~5/min) makes ~2 years roughly 500 calls, about
      1.7 hours; the progress line's ETA accounts for it.

    Safe to Ctrl-C and re-run: every date commits in its own short
    transaction (no long transaction is ever held -- deliberate, see the
    contention work elsewhere in this codebase), already-covered dates (any
    stored bars for that session) are skipped on resume, and the provider's
    per-date response cache makes a re-fetched date free. ``--force``
    re-fetches covered dates and replaces those symbols' rows in place --
    scoped to symbols Polygon actually returned, so e.g. TSX bars sourced
    from the tipranks/yahoo cascade are never destroyed by a force pass.
    """
    cfg = _load(config)
    import time as _time

    from sqlalchemy import select

    from claudetrade.data.universe import load_packaged_universe
    from claudetrade.db.migrations import init_database
    from claudetrade.db.models import PriceBar, Security
    from claudetrade.db.session import get_database
    from claudetrade.providers.base import ProviderError, RateLimitError
    from claudetrade.providers.market.polygon import PolygonProvider
    from claudetrade.utils.timeutils import trading_day_range

    provider = PolygonProvider(config=cfg.polygon, cache_dir=cfg.paths.resolve("cache_dir"))
    status = provider.status()
    # An absent Polygon key selects the keyless path -- it is NOT an error.
    # This used to exit(1) here, which put the only fix for the scanner's
    # insufficient_history blocker behind an API signup and made the setup
    # script fail outright on a clean install.
    use_grouped = status.configured

    end_date = _parse_date(end, current_trading_session())
    start_date = _parse_date(start, end_date - dt.timedelta(days=round(years * 365.25)))
    if start_date > end_date:
        raise typer.BadParameter(f"--start {start_date} is after --end {end_date}")

    db = get_database(cfg)
    init_database(db)

    # Persist only the names the scanner can ever use -- the grouped response
    # is the ENTIRE US market (~10k rows/day) and storing all of it would
    # triple the database for symbols nothing reads. Stored securities
    # (delisted included -- survivorship-unbiased backtests need them), or the
    # packaged seed universes before the first refresh, plus the benchmark and
    # sector ETFs the regime/relative-strength features read.
    with db.read_session() as session:
        target_symbols = set(session.execute(select(Security.symbol)).scalars())
    if not target_symbols:
        target_symbols = {s.symbol for s in load_packaged_universe()}
    target_symbols.add(cfg.market_data.benchmark_symbol)
    target_symbols.update(cfg.market_data.sector_etfs.values())
    targets = sorted(target_symbols)

    dates = list(reversed(trading_day_range(start_date, end_date)))  # newest -> oldest
    if force:
        covered: set[dt.date] = set()
    else:
        with db.read_session() as session:
            covered = set(
                session.execute(
                    select(PriceBar.session)
                    .where(PriceBar.session >= start_date, PriceBar.session <= end_date)
                    .distinct()
                ).scalars()
            )

    if only_if_missing and len(covered) >= min_sessions:
        typer.secho(
            f"backfill not needed: {len(covered)} session(s) already stored between "
            f"{start_date} and {end_date} (>= --min-sessions {min_sessions}).",
            fg=typer.colors.GREEN,
        )
        return

    if not use_grouped:
        stats = _backfill_via_market_chain(
            cfg, db, targets, start_date, end_date, force=force
        )
        _echo_json({**stats, "path": "market_chain", "sessions_already_stored": len(covered)})
        typer.secho(
            f"Backfill complete: {stats['bars_written']} bars across "
            f"{stats['symbols_covered']} symbols"
            + (
                f" ({stats['symbols_failed']} symbol(s) returned nothing)"
                if stats["symbols_failed"]
                else ""
            )
            + ". Run 'claudetrade scan' -- the strategies need ~60 sessions of history.",
            fg=typer.colors.GREEN,
        )
        return

    rate = max(1, cfg.polygon.rate_limit_per_minute)
    source_tag = "polygon_grouped"
    calls_before = status.calls_made
    fetched_dates = 0
    cache_hits = 0
    skipped = 0
    error_dates = 0
    consecutive_errors = 0
    bars_written = 0
    symbols_covered: set[str] = set()

    typer.echo(
        f"backfilling {len(dates)} trading date(s) {start_date}..{end_date} "
        f"(newest first) for {len(targets)} symbol(s); "
        f"{len(covered)} date(s) already covered will be skipped"
    )

    def _persist_date(date: dt.date, bars_by_symbol: dict) -> int:
        """One date -> one short transaction. Returns rows written/updated."""
        written = 0
        with db.session() as session:
            existing_rows: dict[str, list[PriceBar]] = {}
            if force:
                # Replace in place, scoped to symbols Polygon returned --
                # never touches rows for symbols it has no data for.
                for row in session.execute(
                    select(PriceBar).where(PriceBar.session == date)
                ).scalars():
                    if row.symbol in bars_by_symbol:
                        existing_rows.setdefault(row.symbol, []).append(row)
            for symbol, bar in bars_by_symbol.items():
                rows = existing_rows.get(symbol, [])
                if rows:
                    keep = rows[0]
                    keep.open, keep.high, keep.low, keep.close = (
                        bar.open, bar.high, bar.low, bar.close,
                    )
                    keep.adj_close = bar.adj_close
                    keep.volume = bar.volume
                    keep.source = source_tag
                    keep.ingested_at = utc_now()
                    # A second row for the same (symbol, session) from another
                    # source would double-count in the context builder's bar
                    # series -- collapse to one.
                    for extra in rows[1:]:
                        session.delete(extra)
                else:
                    session.add(
                        PriceBar(
                            symbol=symbol,
                            session=date,
                            open=bar.open,
                            high=bar.high,
                            low=bar.low,
                            close=bar.close,
                            adj_close=bar.adj_close,
                            volume=bar.volume,
                            source=source_tag,
                        )
                    )
                written += 1
            return written

    try:
        for index, date in enumerate(dates, start=1):
            if date in covered:
                skipped += 1
            else:
                calls_seen = provider.status().calls_made
                try:
                    bars_by_symbol = provider.grouped_daily_bars(
                        targets, date, bypass_cache=force
                    )
                except RateLimitError as exc:
                    # Honour the server's backoff once, then retry this date;
                    # a second 429 aborts cleanly (resume later).
                    wait = min(exc.retry_after_s, 120.0)
                    typer.secho(
                        f"{date}: rate limited; waiting {wait:.0f}s before retrying",
                        fg=typer.colors.YELLOW,
                    )
                    _time.sleep(wait)
                    bars_by_symbol = provider.grouped_daily_bars(
                        targets, date, bypass_cache=force
                    )
                except ProviderError as exc:
                    error_dates += 1
                    consecutive_errors += 1
                    typer.secho(f"{date}: {exc}", fg=typer.colors.YELLOW)
                    if consecutive_errors >= 5:
                        typer.secho(
                            "5 consecutive date failures -- stopping; progress so far is "
                            "committed, re-run to resume from where this left off.",
                            fg=typer.colors.RED,
                        )
                        raise typer.Exit(1) from exc
                    continue
                consecutive_errors = 0
                if provider.status().calls_made > calls_seen:
                    fetched_dates += 1
                else:
                    cache_hits += 1
                if bars_by_symbol:
                    bars_written += _persist_date(date, bars_by_symbol)
                    symbols_covered.update(bars_by_symbol)
                else:
                    typer.secho(
                        f"{date}: polygon returned no data (EOD not yet published, or an "
                        "unmodelled closure); will retry on a future run",
                        fg=typer.colors.YELLOW,
                    )

            if index % 5 == 0 or index == len(dates):
                remaining_fetch = sum(
                    1 for d in dates[index:] if force or d not in covered
                )
                eta_min = remaining_fetch * (60.0 / rate) / 60.0
                typer.echo(
                    f"  {index}/{len(dates)} dates | {fetched_dates} fetched, "
                    f"{cache_hits} cache hits, {skipped} already covered | "
                    f"{bars_written} bars written | ETA <= {eta_min:.0f} min"
                )
    except KeyboardInterrupt:
        typer.secho(
            "\ninterrupted -- every completed date is already committed; re-run "
            "'claudetrade db backfill' to resume (covered dates are skipped).",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(130) from None

    _echo_json(
        {
            "dates_processed": len(dates),
            "dates_fetched": fetched_dates,
            "dates_from_cache": cache_hits,
            "dates_skipped_covered": skipped,
            "dates_errored": error_dates,
            "http_calls": provider.status().calls_made - calls_before,
            "bars_written": bars_written,
            "symbols_covered": len(symbols_covered),
        }
    )
    typer.secho(
        f"Backfill complete: {bars_written} bars across {len(symbols_covered)} symbols "
        f"({fetched_dates} dates fetched, {cache_hits} from cache, {skipped} already "
        "covered). Run 'claudetrade scan' -- the strategies need ~60 sessions of "
        "history, which this has now provided.",
        fg=typer.colors.GREEN,
    )


@sentiment_app.command("history")
def sentiment_history(
    symbol: Annotated[str, typer.Argument(help="Ticker to report, e.g. NVDA.")],
    config: ConfigOption = None,
    days: Annotated[int, typer.Option(help="Calendar days of history to cover.")] = 90,
    as_of: Annotated[
        str | None, typer.Option(help="ISO session to report as of; defaults to the current one.")
    ] = None,
) -> None:
    """One symbol's daily mention and sentiment series.

    Trading sessions only, gap-filled: a session with no stored row reports
    zero mentions with ``observed: false``, so "nobody talked about it" is
    distinguishable from "we have no data" while the series stays
    continuous enough to do arithmetic on.
    """
    cfg = _load(config)
    from claudetrade.db.session import get_database
    from claudetrade.sentiment.history import symbol_series

    session_date = _parse_date(as_of, current_trading_session())
    window = symbol_series(
        get_database(cfg), symbol, as_of=session_date, days=days
    )
    _echo_json(window.to_dict())


@sentiment_app.command("rising")
def sentiment_rising(
    config: ConfigOption = None,
    days: Annotated[int, typer.Option(help="Ignored placeholder for symmetry.")] = 90,
    recent: Annotated[int, typer.Option(help="Sessions in the recent window.")] = 3,
    baseline: Annotated[int, typer.Option(help="Sessions in the comparison baseline.")] = 20,
    limit: Annotated[int, typer.Option(help="Rows to display.")] = 25,
    min_mentions: Annotated[
        int, typer.Option(help="Minimum recent mentions before a symbol can rank.")
    ] = 5,
    as_of: Annotated[
        str | None, typer.Option(help="ISO session to rank as of; defaults to the current one.")
    ] = None,
) -> None:
    """Symbols whose chatter is accelerating against their own baseline.

    The screen this application exists to run: recent mention *rate* versus
    the symbol's own prior rate, so a quiet name waking up outranks a
    permanently-loud one. Sentiment change is reported beside each row but
    never ranked on -- attention rises first, tone tells you which way to
    read it, and a mention surge with collapsing tone is a short candidate
    rather than a row to hide.
    """
    cfg = _load(config)
    from claudetrade.db.session import get_database
    from claudetrade.sentiment.history import coverage_summary, rising_symbols

    db = get_database(cfg)
    session_date = _parse_date(as_of, current_trading_session())
    coverage = coverage_summary(db, as_of=session_date, days=days)
    trends = rising_symbols(
        db,
        as_of=session_date,
        recent_sessions=recent,
        baseline_sessions=baseline,
        limit=limit,
        min_recent_mentions=min_mentions,
    )
    _echo_json(
        {
            "as_of": session_date.isoformat(),
            "recent_sessions": recent,
            "baseline_sessions": baseline,
            "coverage": coverage,
            "count": len(trends),
            "rising": [t.to_dict() for t in trends],
        }
    )
    if coverage["sessions_with_data"] < baseline:
        typer.secho(
            f"Only {coverage['sessions_with_data']} session(s) of stored history -- fewer "
            f"than the {baseline}-session baseline. Trends are provisional until history "
            "accumulates (one session per refresh; ApeWisdom attention rows carry their "
            "own 24h comparison and are usable sooner).",
            fg=typer.colors.YELLOW,
        )


@db_app.command("fetch-health")
def db_fetch_health(
    config: ConfigOption = None,
    clear: Annotated[
        str | None, typer.Option("--clear", help="Clear one symbol's failure/quarantine record.")
    ] = None,
    clear_all: Annotated[
        bool, typer.Option("--clear-all", help="Clear every failure/quarantine record.")
    ] = False,
) -> None:
    """List (or clear) symbols the refresh has quarantined for repeated
    full-provider-chain fetch failures.

    A row exists only while a symbol is failing (success deletes it -- see
    ``db.models.SymbolFetchHealth``); after 3 consecutive refreshes with no
    bars from any configured provider the symbol is skipped for 7 days
    rather than burning doomed per-symbol calls every refresh. Clearing a
    record makes the next refresh try the symbol again immediately.
    """
    cfg = _load(config)
    from sqlalchemy import delete, select

    from claudetrade.db.migrations import init_database
    from claudetrade.db.models import SymbolFetchHealth
    from claudetrade.db.session import get_database

    db = get_database(cfg)
    init_database(db)

    if clear is not None and clear_all:
        raise typer.BadParameter("use either --clear SYMBOL or --clear-all, not both")

    if clear is not None:
        symbol = clear.strip().upper()
        with db.session() as session:
            removed = session.execute(
                delete(SymbolFetchHealth).where(SymbolFetchHealth.symbol == symbol)
            ).rowcount
        if removed:
            typer.secho(
                f"cleared {symbol}; the next refresh will fetch it again.",
                fg=typer.colors.GREEN,
            )
        else:
            typer.secho(f"no fetch-health record for {symbol}.", fg=typer.colors.YELLOW)
        return

    if clear_all:
        with db.session() as session:
            removed = session.execute(delete(SymbolFetchHealth)).rowcount
        typer.secho(f"cleared {removed} fetch-health record(s).", fg=typer.colors.GREEN)
        return

    with db.read_session() as session:
        rows = (
            session.execute(
                select(SymbolFetchHealth).order_by(
                    SymbolFetchHealth.consecutive_failures.desc(), SymbolFetchHealth.symbol
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        typer.secho(
            "No failing symbols -- every recently-refreshed symbol got bars from at "
            "least one provider.",
            fg=typer.colors.GREEN,
        )
        return

    now = utc_now()

    def _aware(value: dt.datetime | None) -> dt.datetime | None:
        # SQLite returns naive datetimes; writes always went through UTC.
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value

    quarantined = 0
    typer.echo(f"{'SYMBOL':<10} {'FAILS':>5}  {'QUARANTINED UNTIL':<20} LAST ERROR")
    for row in rows:
        until = _aware(row.quarantined_until)
        active = until is not None and until > now
        quarantined += 1 if active else 0
        until_text = until.date().isoformat() if active else "-"
        typer.echo(
            f"{row.symbol:<10} {row.consecutive_failures:>5}  {until_text:<20} "
            f"{row.last_error[:70]}"
        )
    typer.echo(
        f"\n{len(rows)} failing symbol(s), {quarantined} currently quarantined. "
        "Clear one with --clear SYMBOL, or all with --clear-all."
    )


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
def ui(
    config: ConfigOption = None,
    port: int | None = None,
    classic: bool = typer.Option(
        False,
        "--classic",
        help="Launch the legacy Streamlit interface instead of the desktop app.",
    ),
) -> None:
    """Launch the desktop interface (React app; --classic for Streamlit)."""
    cfg = _load(config)

    if not classic:
        # The ADR-0008 web UI: FastAPI + built React SPA in a native window
        # (browser fallback). It is its own module entry point so a frozen
        # build can also launch it without re-executing the bootloader.
        import subprocess

        command = [sys.executable, "-m", "claudetrade.webapi"]
        if port:
            command += ["--port", str(port)]
        if getattr(sys, "frozen", False):
            from claudetrade.webapi.__main__ import main as webapi_main

            raise typer.Exit(webapi_main(["--port", str(port)] if port else []))
        raise typer.Exit(subprocess.call(command))

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


@app.command("mcp")
def mcp_server(config: ConfigOption = None) -> None:
    r"""Run a local MCP (Model Context Protocol) server over stdio.

    Lets an MCP client on this machine -- typically the Claude Desktop app,
    configured via its ``claude_desktop_config.json`` -- query this
    installation's signals, sentiment and market status directly, without
    the web UI running. See docs/claude-desktop-mcp.md for setup.

    Bootstraps its own ``Pipeline`` (``Pipeline.bootstrap``, same as every
    other entry point), so it works whether or not ``claudetrade ui`` is
    already running -- SQLite's WAL mode makes concurrent read access safe.
    This command blocks, serving requests on stdin/stdout, until the client
    disconnects; all diagnostic output here goes to stderr so it never
    collides with the MCP protocol framing on stdout.

    Requires the optional ``mcp`` package (``pip install claudetrade\[mcp]``);
    everything else in the application works without it.
    """
    cfg = get_config(config, reload=True)
    setup_logging(cfg, component="mcp")

    try:
        from claudetrade.mcp_server import run_stdio
    except ImportError as exc:
        typer.secho(
            "The 'mcp' package is not installed. Install it with:\n"
            "  pip install claudetrade[mcp]\n"
            "(or: pip install mcp)\n\n"
            f"Details: {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from exc

    typer.echo(f"claudetrade MCP server starting (stdio transport). {DISCLAIMER}", err=True)
    run_stdio(cfg)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
