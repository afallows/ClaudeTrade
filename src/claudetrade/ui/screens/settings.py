"""Settings page: configuration, secrets, risk limits, and database.

Allows managing API keys (via describe_secrets), data providers, risk
settings, database backups, and scheduler configuration.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from claudetrade.db.backup import create_backup, list_backups
from claudetrade.secrets import (
    describe_secrets,
    set_secret,
)
from claudetrade.ui.formatting import format_currency, show_disclaimer
from claudetrade.ui.state import get_config, get_pipeline


def page_settings() -> None:
    """Render the settings page."""
    st.set_page_config(page_title="Settings", layout="wide")
    st.title("⚙️ Settings")
    show_disclaimer()

    config = get_config()
    pipeline = get_pipeline(config)

    # --- Tabs ---
    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["Providers", "API Keys", "Risk & Filters", "Database", "Scheduler"]
    )

    # --- Providers ---
    with tab1:
        st.subheader("Data Providers")

        col1, col2 = st.columns(2)
        with col1:
            st.write("**Market Data**")
            st.write(f"Provider: {config.market_data.provider}")
            st.write(f"Benchmark: {config.market_data.benchmark_symbol}")
            st.write(f"Max Symbols per Request: {config.market_data.max_symbols_per_request}")

        with col2:
            st.write("**Earnings**")
            st.write(f"Provider: {config.earnings.provider}")

        col1, col2 = st.columns(2)
        with col1:
            st.write("**Reddit**")
            st.write(f"Enabled: {'Yes' if config.reddit.enabled else 'No'}")
            if config.reddit.enabled:
                st.write(f"Subreddits: {', '.join(config.reddit.subreddits[:3])}...")
                st.write(f"Lookback: {config.reddit.lookback_hours}h")

        with col2:
            st.write("**X (Twitter)**")
            st.write(f"Enabled: {'Yes' if config.x.enabled else 'No'}")
            if config.x.enabled:
                st.write(f"Lookback: {config.x.lookback_hours}h")
                st.write(f"Max Results per Query: {config.x.max_results_per_query}")

        col1, col2 = st.columns(2)
        with col1:
            st.write("**AI Sentiment**")
            st.write(f"Provider: {config.ai.provider}")
            if config.ai.provider != "null":
                st.write(f"Model: {config.ai.model}")
                st.write(f"Cache Enabled: {config.ai.cache_enabled}")

        with col2:
            st.write("**Notifications**")
            st.write(f"Enabled: {'Yes' if config.notifications.enabled else 'No'}")
            if config.notifications.enabled:
                st.write(f"Channels: {', '.join(config.notifications.channels)}")

    # --- API Keys ---
    with tab2:
        st.subheader("API Credential Management")

        # List of configured credentials
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

        st.write("**Configured Credentials** (never displays keys, only status)")

        status = describe_secrets([name for name, _ in credentials_to_check])

        for cred_name, display_name in credentials_to_check:
            col1, col2, col3, col4 = st.columns(4)
            info = status.get(cred_name, {})
            configured = info.get("configured") == "yes"

            with col1:
                st.write(display_name)
            with col2:
                if configured:
                    st.success("✅ Configured")
                else:
                    st.warning("⚠️ Not Set")
            with col3:
                if configured:
                    st.write(f"Source: {info.get('source', '-')}")
            with col4:
                if configured:
                    st.write(f"Masked: {info.get('masked', '****')}")

        # Set new credential
        st.write("**Set or Update Credential**")
        col1, col2 = st.columns(2)

        def get_display_name(cred_name: str) -> str:
            """Get the display name for a credential."""
            return next(
                (d for n, d in credentials_to_check if n == cred_name),
                cred_name
            )

        with col1:
            selected_cred = st.selectbox(
                "Select credential to set:",
                options=[name for name, _ in credentials_to_check],
                format_func=get_display_name,
                key="credential_select",
            )

        with col2:
            new_value = st.text_input(
                "Value",
                type="password",
                key="credential_value",
            )

        if st.button("Save Credential", key="save_credential"):
            if not new_value:
                st.error("Credential value cannot be empty")
            else:
                try:
                    set_secret(selected_cred, new_value)
                    st.success(f"✅ Credential '{selected_cred}' has been saved")
                except Exception as e:
                    st.error(f"Failed to save credential: {e}")

    # --- Risk & Filters ---
    with tab3:
        st.subheader("Risk Configuration")

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
            st.metric("Max Correlation Exposure", f"{config.risk.max_correlated_exposure_pct:.2f}%")

        st.subheader("Candidate Filters")

        col1, col2, col3 = st.columns(3)
        with col1:
            st.write("**Price & Liquidity**")
            st.write(f"Min Price: ${config.filters.min_price:.2f}")
            st.write(f"Max Price: ${config.filters.max_price:.2f}")
            st.write(f"Min Market Cap: {format_currency(config.filters.min_market_cap_usd)}")
            st.write(f"Min ADV: {format_currency(config.filters.min_avg_dollar_volume_usd)}")

        with col2:
            st.write("**Sentiment & Quality**")
            st.write(f"Min Unique Authors: {config.filters.min_unique_authors}")
            st.write(f"Min Confidence: {config.filters.min_sentiment_confidence:.2f}")
            st.write(f"Max Manipulation Risk: {config.filters.max_manipulation_risk:.2f}")

        with col3:
            st.write("**Earnings Guard**")
            st.write(f"Min Days to Earnings: {config.filters.min_days_to_earnings}")
            if config.filters.max_days_to_earnings:
                st.write(f"Max Days to Earnings: {config.filters.max_days_to_earnings}")
            st.write(f"Block Entry Within: {config.filters.block_entry_within_days_of_earnings}d")

    # --- Database ---
    with tab4:
        st.subheader("Database Management")

        col1, col2 = st.columns(2)
        with col1:
            st.write("**Database Location**")
            st.write(f"URL: {config.database_url()}")
            st.write(f"SQLite WAL: {'Enabled' if config.database.sqlite_wal else 'Disabled'}")

        with col2:
            st.write("**Configuration**")
            st.write(f"Pool Size: {config.database.pool_size}")
            st.write(f"Busy Timeout: {config.database.busy_timeout_ms}ms")

        # Backups
        st.subheader("Backups")

        if st.button("Create Backup", key="create_backup"):
            with st.spinner("Creating backup..."):
                try:
                    backup_dir = config.paths.resolve("backups_dir")
                    backup_path = create_backup(pipeline.db, backup_dir)
                    st.success(f"✅ Backup created: {backup_path.name}")
                except Exception as e:
                    st.error(f"Backup failed: {e}")

        st.write("**Recent Backups**")
        try:
            backup_dir = config.paths.resolve("backups_dir")
            backups = list_backups(backup_dir)
            if backups:
                for backup in backups[:10]:
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        st.write(backup.name)
                    with col2:
                        size_mb = backup.stat().st_size / (1024 * 1024)
                        st.write(f"{size_mb:.1f} MB")
                    with col3:
                        mtime = dt.datetime.fromtimestamp(
                            backup.stat().st_mtime,
                            tz=dt.UTC
                        )
                        st.write(mtime.strftime("%Y-%m-%d %H:%M"))
            else:
                st.info("No backups yet")
        except Exception as e:
            st.warning(f"Could not list backups: {e}")

    # --- Scheduler ---
    with tab5:
        st.subheader("Scheduler Configuration")

        if config.scheduler.enabled:
            st.success("✅ Scheduler Enabled")
            col1, col2 = st.columns(2)
            with col1:
                st.write("**Timezone**")
                st.write(config.scheduler.timezone)
            with col2:
                st.write("**Misfire Grace Time**")
                st.write(f"{config.scheduler.misfire_grace_time_s}s")

            st.write("**Scheduled Jobs**")
            st.write(f"Market Data Refresh: {config.scheduler.market_data_refresh_cron}")
            st.write(f"Social Refresh: {config.scheduler.social_refresh_cron}")
            st.write(f"Scan: {config.scheduler.scan_cron}")
            st.write(f"Paper Mark: {config.scheduler.paper_mark_cron}")
        else:
            st.warning("⚠️ Scheduler Disabled")

    # --- Trading Mode ---
    st.subheader("Trading Mode")
    col1, col2, col3 = st.columns(3)

    with col1:
        st.write("**Mode**")
        mode = config.trading.mode
        if mode == "live":
            st.error(f"🔴 {mode.upper()}")
        elif mode == "paper":
            st.warning(f"🟡 {mode.upper()}")
        else:
            st.info(f"🟢 {mode.upper()}")

    with col2:
        st.write("**Live Trading Authorized**")
        if config.trading.live_trading_authorised:
            st.error("🔴 YES")
        else:
            st.success("🟢 NO")

    with col3:
        st.write("**Broker**")
        if config.trading.broker:
            st.write(config.trading.broker)
        else:
            st.write("None configured")
