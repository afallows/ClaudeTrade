"""Tests for application configuration handling."""

from __future__ import annotations

import pytest

from claudetrade.config import AppConfig, FilterConfig, RiskConfig, TradingModeConfig


class TestConfigDefaults:
    """Default configuration must be safe and valid."""

    def test_defaults_validate(self):
        """Defaults pass all validators without error."""
        config = AppConfig()
        assert config is not None
        assert config.risk.account_size_usd > 0
        assert config.paths.app_dir is not None

    def test_filter_max_price_exceeds_min(self):
        """max_price must exceed min_price in filters."""
        with pytest.raises(ValueError, match="max_price must exceed min_price"):
            FilterConfig(min_price=100, max_price=50)

    def test_risk_heat_covers_trade(self):
        """max_portfolio_heat_pct must be >= max_risk_per_trade_pct."""
        with pytest.raises(
            ValueError, match="max_portfolio_heat_pct must be >= max_risk_per_trade_pct"
        ):
            RiskConfig(max_risk_per_trade_pct=5.0, max_portfolio_heat_pct=2.0)

    def test_live_requires_authorisation(self):
        """mode='live' requires live_trading_authorised=true."""
        with pytest.raises(ValueError, match="mode='live' requires live_trading_authorised"):
            TradingModeConfig(mode="live", live_trading_authorised=False)

    def test_live_requires_broker(self):
        """mode='live' requires a broker to be configured."""
        with pytest.raises(ValueError, match="requires a configured broker"):
            TradingModeConfig(mode="live", live_trading_authorised=True, broker=None)


class TestConfigValidation:
    """Configuration values are validated at assignment."""

    def test_percentages_must_be_positive(self):
        """Risk percentages must be in (0, 100]."""
        with pytest.raises(ValueError, match="must be in"):
            RiskConfig(max_risk_per_trade_pct=0.0)

        with pytest.raises(ValueError, match="must be in"):
            RiskConfig(max_risk_per_trade_pct=150.0)

    def test_filter_confidence_must_be_unit_interval(self):
        """Confidence thresholds must be in [0, 1]."""
        with pytest.raises(ValueError, match="must be within"):
            FilterConfig(min_sentiment_confidence=1.5)

    def test_manipulation_risk_must_be_unit_interval(self):
        """Manipulation risk threshold must be in [0, 1]."""
        with pytest.raises(ValueError, match="must be within"):
            FilterConfig(max_manipulation_risk=2.0)


class TestConfigHash:
    """Configuration hash is stable and changes with settings."""

    def test_config_hash_is_stable(self):
        """Same configuration produces same hash."""
        config1 = AppConfig()
        config2 = AppConfig()
        assert config1.config_hash == config2.config_hash

    def test_config_hash_changes_with_settings(self):
        """Changing a setting changes the hash."""
        config1 = AppConfig()
        config2 = AppConfig()
        config2.risk.account_size_usd = 200_000.0

        assert config1.config_hash != config2.config_hash


class TestConfigEnvironmentOverrides:
    """Environment variables with CLAUDETRADE_ prefix and __ nesting override file/defaults."""

    def test_env_var_override_scalar(self, monkeypatch):
        """CLAUDETRADE_RISK__ACCOUNT_SIZE_USD overrides scalar config."""
        monkeypatch.setenv("CLAUDETRADE_RISK__ACCOUNT_SIZE_USD", "250000")
        config = AppConfig()
        assert config.risk.account_size_usd == 250_000.0

    def test_env_var_override_nested(self, monkeypatch):
        """Nested env vars with __ separator work."""
        monkeypatch.setenv("CLAUDETRADE_RISK__MAX_RISK_PER_TRADE_PCT", "1.5")
        config = AppConfig()
        assert config.risk.max_risk_per_trade_pct == 1.5

    def test_env_var_override_boolean(self, monkeypatch):
        """Boolean config from environment."""
        monkeypatch.setenv("CLAUDETRADE_LOGGING__JSON_FORMAT", "False")
        config = AppConfig()
        assert config.logging.json_format is False


class TestConfigLoad:
    """Configuration can be loaded from a TOML file."""

    def test_load_from_file(self, tmp_path):
        """AppConfig.load reads a TOML file."""
        config_file = tmp_path / "test_config.toml"
        config_file.write_text(
            """
[risk]
account_size_usd = 500000
max_risk_per_trade_pct = 1.0
"""
        )

        config = AppConfig.load(config_file)
        assert config.risk.account_size_usd == 500_000
        assert config.risk.max_risk_per_trade_pct == 1.0

    def test_load_missing_file_raises(self, tmp_path):
        """Loading a non-existent file raises FileNotFoundError."""
        config_file = tmp_path / "missing.toml"
        with pytest.raises(FileNotFoundError):
            AppConfig.load(config_file)

    def test_load_missing_file_not_required(self):
        """Load without path uses defaults if no config file exists."""
        config = AppConfig.load()
        assert config is not None
        assert config.risk.account_size_usd > 0


class TestConfigPublicDict:
    """public_dict excludes path details."""

    def test_public_dict_excludes_paths(self):
        """Paths are excluded from public_dict."""
        config = AppConfig()
        pub = config.public_dict()
        assert "paths" not in pub
