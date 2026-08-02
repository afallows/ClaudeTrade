"""Tests for ``claudetrade config path|init|show``.

These exist because a missing ``config.toml`` is not an error anywhere in
this application -- built-in defaults run fine -- so its absence is silent
and its location unguessable. Settings with no credential-store and no UI
path (``x.session_query_id``, ``x.session_symbols``) were therefore
unreachable in practice: an operator had to already know the OS-specific
directory, the section name and the key name to set them at all.
"""

from __future__ import annotations

import tomllib

import pytest
from typer.testing import CliRunner

from claudetrade.cli import app

runner = CliRunner()


@pytest.fixture
def app_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDETRADE_CONFIG", raising=False)
    from claudetrade.config import reset_config_cache

    reset_config_cache()
    return tmp_path


class TestConfigPath:
    def test_reports_the_default_location_and_that_it_is_absent(self, app_home):
        result = runner.invoke(app, ["config", "path"])
        assert result.exit_code == 0
        assert str(app_home / "config.toml") in result.output
        assert "does not exist" in result.output

    def test_absent_output_says_how_to_create_it(self, app_home):
        """A path with no next step still leaves the operator hand-writing a
        file at a location they have to trust they got right."""
        result = runner.invoke(app, ["config", "path"])
        assert "claudetrade config init" in result.output

    def test_reports_existence_once_created(self, app_home):
        runner.invoke(app, ["config", "init"])
        result = runner.invoke(app, ["config", "path"])
        assert "status: exists" in result.output
        assert "does not exist" not in result.output

    def test_env_override_is_reported_as_the_source(self, app_home, monkeypatch, tmp_path):
        """``$CLAUDETRADE_CONFIG`` wins over the default, and the operator
        needs to be told *which* rule produced the path they are looking at."""
        override = tmp_path / "elsewhere" / "custom.toml"
        monkeypatch.setenv("CLAUDETRADE_CONFIG", str(override))
        result = runner.invoke(app, ["config", "path"])
        assert str(override) in result.output
        assert "CLAUDETRADE_CONFIG" in result.output


class TestConfigInit:
    def test_writes_a_parseable_file_at_the_reported_path(self, app_home):
        result = runner.invoke(app, ["config", "init"])
        assert result.exit_code == 0
        written = app_home / "config.toml"
        assert written.exists()
        with written.open("rb") as fh:
            tomllib.load(fh)  # must not raise

    def test_generated_file_changes_no_behaviour(self, app_home):
        """Every setting ships commented out, so running this on a working
        install is safe -- it gives the settings a home, it does not apply
        any. Anything uncommented here would silently pin a value that was
        previously free to move with the defaults."""
        runner.invoke(app, ["config", "init"])
        with (app_home / "config.toml").open("rb") as fh:
            parsed = tomllib.load(fh)
        assert all(section == {} for section in parsed.values()), parsed

    def test_documents_the_settings_that_have_no_other_home(self, app_home):
        """X session mode is the case that motivated this command: both of
        its required settings are config-only."""
        runner.invoke(app, ["config", "init"])
        text = (app_home / "config.toml").read_text()
        assert "session_query_id" in text
        assert "session_symbols" in text
        assert "SearchTimeline" in text

    def test_says_credentials_do_not_belong_here(self, app_home):
        """The file is the obvious-looking place to paste an API key, and
        doing so silently does nothing -- ``AppConfig.load`` drops it."""
        runner.invoke(app, ["config", "init"])
        assert "Credentials NEVER go in this file" in (app_home / "config.toml").read_text()

    def test_refuses_to_clobber_an_existing_file(self, app_home):
        existing = app_home / "config.toml"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text('[signals]\nmin_confidence = 0.9\n')

        result = runner.invoke(app, ["config", "init"])
        assert result.exit_code == 0
        assert "already exists" in result.output
        assert existing.read_text() == '[signals]\nmin_confidence = 0.9\n'

    def test_force_replaces_but_keeps_a_backup(self, app_home):
        existing = app_home / "config.toml"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text('[signals]\nmin_confidence = 0.9\n')

        result = runner.invoke(app, ["config", "init", "--force"])
        assert result.exit_code == 0
        assert "session_query_id" in existing.read_text()
        assert (app_home / "config.toml.bak").read_text() == '[signals]\nmin_confidence = 0.9\n'


class TestConfigShow:
    def test_prints_effective_config_and_where_it_came_from(self, app_home):
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert str(app_home / "config.toml") in result.output
        assert "not found" in result.output

    def test_redacts_credential_values(self, app_home):
        """``public_dict`` is the redaction boundary; this pins that ``config
        show`` uses it rather than dumping the model."""
        result = runner.invoke(app, ["config", "show"])
        assert "REDACTED" in result.output or "api_key" not in result.output.split("polygon")[-1][:200]
