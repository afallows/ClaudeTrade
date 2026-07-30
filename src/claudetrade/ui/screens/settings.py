"""Settings: effective configuration (read-only), provider probes, credentials,
database, and trading-mode status.

Only credentials have a real write path (the OS keyring, via
``claudetrade.secrets.set_secret``) -- every other field on this screen is
display-only, with the ``config.toml`` path shown, because ``AppConfig`` has
no in-app save method today. A control that looked editable but silently did
nothing was flagged before; this screen never repeats that pattern.
"""

from __future__ import annotations

import datetime as dt
import os

import streamlit as st

from claudetrade.config import ENV_PREFIX, default_app_dir
from claudetrade.db.backup import create_backup, list_backups
from claudetrade.secrets import describe_secrets, set_secret
from claudetrade.ui.components.layout import page_header
from claudetrade.ui.components.tables import empty_state
from claudetrade.ui.formatting import format_currency
from claudetrade.ui.state import get_config, get_pipeline


def resolved_config_path() -> str:
    """Where ``AppConfig.load()`` would read from, mirroring its own resolution order."""
    env_override = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if env_override:
        return f"{env_override} (from ${ENV_PREFIX}CONFIG)"
    default = default_app_dir() / "config.toml"
    if default.exists():
        return str(default)
    return f"{default} (not found -- running on built-in defaults)"


def page_settings() -> None:
    """Render the settings page."""
    config = get_config()
    pipeline = get_pipeline(config)
    page_header("⚙️", "Settings", "Effective configuration, provider probes, and account status.")

    st.caption(f"Config file: `{resolved_config_path()}`  ·  Config hash: `{config.config_hash[:16]}`")

    tabs = st.tabs(
        ["Providers", "API Keys", "Risk &amp; Filters", "Database", "Scheduler", "Trading Mode"]
    )

    with tabs[0]:
        _render_providers(pipeline)
    with tabs[1]:
        _render_api_keys()
    with tabs[2]:
        _render_risk_and_filters(config)
    with tabs[3]:
        _render_database(config, pipeline)
    with tabs[4]:
        _render_scheduler(config)
    with tabs[5]:
        _render_trading_mode(config)


def _render_providers(pipeline) -> None:
    st.subheader("Data Providers -- Live Probe")
    st.caption(
        "Each row is a live probe result (the provider is instantiated and its own "
        "`.status()` is called), not a static config dump."
    )
    try:
        statuses = pipeline.provider_status()
    except Exception as exc:
        st.error(f"Could not probe providers: {exc}")
        return
    if not statuses:
        empty_state("No providers configured.")
        return
    rows = [
        {
            "Provider": s.name,
            "Kind": s.kind,
            "Available": "yes" if s.available else "no",
            "Configured": "yes" if s.configured else "no",
            "Point-in-time": "yes" if s.supports_point_in_time else "no",
            "Delisted history": "yes" if s.supports_delisted else "no",
            "Rate limit /min": s.rate_limit_per_minute if s.rate_limit_per_minute else "-",
            "Message": s.message,
            "Licence note": s.licence_note or "-",
        }
        for s in statuses
    ]
    st.dataframe(rows, hide_index=True, width="stretch")


def _render_api_keys() -> None:
    st.subheader("API Credential Management")
    st.caption("Values are never displayed -- only whether a credential resolves, from where, and a masked tail.")

    credentials_to_check = [
        ("anthropic_api_key", "Anthropic API Key"),
        ("openai_api_key", "OpenAI API Key"),
        ("reddit_client_id", "Reddit Client ID"),
        ("reddit_client_secret", "Reddit Client Secret"),
        ("x_bearer_token", "X Bearer Token"),
        ("notify_webhook_url", "Webhook URL"),
        ("smtp_user", "SMTP Username"),
        ("smtp_password", "SMTP Password"),
    ]
    status = describe_secrets([name for name, _ in credentials_to_check])
    rows = [
        {
            "Credential": display_name,
            "Status": "configured" if status.get(name, {}).get("configured") == "yes" else "not set",
            "Source": status.get(name, {}).get("source", "-"),
            "Masked": status.get(name, {}).get("masked", "-"),
        }
        for name, display_name in credentials_to_check
    ]
    st.dataframe(rows, hide_index=True, width="stretch")

    st.write("**Set or update a credential** (written to the OS credential store)")
    col1, col2 = st.columns(2)
    display_by_name = dict(credentials_to_check)
    with col1:
        selected_cred = st.selectbox(
            "Credential",
            options=[name for name, _ in credentials_to_check],
            format_func=lambda n: display_by_name.get(n, n),
            key="credential_select",
        )
    with col2:
        new_value = st.text_input("Value", type="password", key="credential_value")

    if st.button("Save Credential", key="save_credential"):
        if not new_value:
            st.error("Credential value cannot be empty.")
        else:
            try:
                backend = set_secret(selected_cred, new_value)
                st.success(f"Saved '{selected_cred}' to the {backend} store.")
            except Exception as exc:
                st.error(f"Failed to save credential: {exc}")


def _render_risk_and_filters(config) -> None:
    st.subheader("Risk &amp; Position Sizing")
    st.caption(
        f"Display-only: edit `{resolved_config_path()}` and restart the app to change these. "
        "There is no in-app write path for this section yet."
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        st.write("**Account**")
        st.metric("Account Size", format_currency(config.risk.account_size_usd))
        st.metric("Max Position Size", f"{config.risk.max_position_size_pct:.1f}%")
        st.metric("Max Concurrent Positions", config.risk.max_concurrent_positions)
    with col2:
        st.write("**Per-Trade Risk**")
        st.metric("Max Risk Per Trade", f"{config.risk.max_risk_per_trade_pct:.2f}%")
        st.metric("Min Reward:Risk", f"{config.risk.min_reward_risk_ratio:.2f}:1")
        st.metric("Max Daily Loss", f"{config.risk.max_daily_loss_pct:.2f}%")
    with col3:
        st.write("**Portfolio Heat**")
        st.metric("Max Portfolio Heat", f"{config.risk.max_portfolio_heat_pct:.2f}%")
        st.metric("Max Sector Exposure", f"{config.risk.max_sector_exposure_pct:.2f}%")
        st.metric("Max Correlated Exposure", f"{config.risk.max_correlated_exposure_pct:.2f}%")

    st.subheader("Candidate Filters")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.write("**Price &amp; Liquidity**")
        st.write(f"Min Price: {format_currency(config.filters.min_price)}")
        st.write(f"Max Price: {format_currency(config.filters.max_price)}")
        st.write(f"Min Market Cap: {format_currency(config.filters.min_market_cap_usd)}")
        st.write(f"Min ADV: {format_currency(config.filters.min_avg_dollar_volume_usd)}")
    with col2:
        st.write("**Sentiment &amp; Quality**")
        st.write(f"Min Unique Authors: {config.filters.min_unique_authors}")
        st.write(f"Min Sentiment Confidence: {config.filters.min_sentiment_confidence:.2f}")
        st.write(f"Max Manipulation Risk: {config.filters.max_manipulation_risk:.2f}")
    with col3:
        st.write("**Earnings Guard**")
        st.write(f"Min Days to Earnings: {config.filters.min_days_to_earnings}")
        if config.filters.max_days_to_earnings:
            st.write(f"Max Days to Earnings: {config.filters.max_days_to_earnings}")
        st.write(f"Block Entry Within: {config.filters.block_entry_within_days_of_earnings}d of earnings")

    st.subheader("Signal Generation")
    col1, col2 = st.columns(2)
    with col1:
        st.write(f"Enabled strategies: {', '.join(config.signals.enabled_strategies)}")
        st.write(f"Allow shorts: {'yes' if config.signals.allow_shorts else 'no'}")
    with col2:
        st.write(f"Min overall score: {config.signals.min_overall_score:.0f}")
        st.write(f"Min confidence: {config.signals.min_confidence:.2f}")


def _render_database(config, pipeline) -> None:
    st.subheader("Database")
    col1, col2 = st.columns(2)
    with col1:
        st.write(f"**URL**: `{config.database_url()}`")
        st.write(f"**SQLite WAL**: {'enabled' if config.database.sqlite_wal else 'disabled'}")
    with col2:
        st.write(f"**Pool size**: {config.database.pool_size}")
        st.write(f"**Busy timeout**: {config.database.busy_timeout_ms}ms")

    st.write("**Backups** (writes a real snapshot file)")
    if st.button("Create Backup", key="create_backup"):
        with st.spinner("Creating backup..."):
            try:
                backup_dir = config.paths.resolve("backups_dir")
                backup_path = create_backup(pipeline.db, backup_dir)
                st.success(f"Backup created: {backup_path.name}")
            except Exception as exc:
                st.error(f"Backup failed: {exc}")

    try:
        backup_dir = config.paths.resolve("backups_dir")
        backups = list_backups(backup_dir)
    except Exception as exc:
        st.warning(f"Could not list backups: {exc}")
        return
    if not backups:
        empty_state("No backups yet.", "claudetrade db backup")
        return
    rows = [
        {
            "File": b.name,
            "Size (MB)": round(b.stat().st_size / (1024 * 1024), 2),
            "Created": dt.datetime.fromtimestamp(b.stat().st_mtime, tz=dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
        }
        for b in backups[:10]
    ]
    st.dataframe(rows, hide_index=True, width="stretch")


def _render_scheduler(config) -> None:
    st.subheader("Scheduler")
    st.caption(f"Display-only: edit `{resolved_config_path()}` to change these.")
    if not config.scheduler.enabled:
        st.warning("Scheduler disabled")
        return
    st.success("Scheduler enabled")
    col1, col2 = st.columns(2)
    with col1:
        st.write(f"**Timezone**: {config.scheduler.timezone}")
    with col2:
        st.write(f"**Misfire grace**: {config.scheduler.misfire_grace_time_s}s")
    st.write(f"Market data refresh: `{config.scheduler.market_data_refresh_cron}`")
    st.write(f"Social refresh: `{config.scheduler.social_refresh_cron}`")
    st.write(f"Scan: `{config.scheduler.scan_cron}`")
    st.write(f"Paper mark: `{config.scheduler.paper_mark_cron}`")


def _render_trading_mode(config) -> None:
    st.subheader("Trading Mode")
    col1, col2, col3 = st.columns(3)
    with col1:
        mode = config.trading.mode
        if mode == "live":
            st.error(f"🔴 {mode.upper()}")
        elif mode == "paper":
            st.info(f"🟡 {mode.upper()}")
        else:
            st.success(f"🟢 {mode.upper()}")
    with col2:
        st.write("**Live trading authorised**")
        st.error("YES") if config.trading.live_trading_authorised else st.success("NO")
    with col3:
        st.write("**Broker**")
        st.write(config.trading.broker or "None configured")
    if config.trading.kill_switch_engaged:
        st.error("🔴 Kill switch engaged in config -- new entries are blocked until this is cleared.")
