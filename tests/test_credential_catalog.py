"""The two credential screens must offer the same credentials."""
from __future__ import annotations

from claudetrade.config import AppConfig
from claudetrade.secrets import credential_catalog


class TestCredentialCatalogIsTheSingleSource:
    """Every credential an operator can hold needs somewhere to go.

    Both screens previously kept their own hand-written list and both were
    missing entries the other had -- a key with no field is indistinguishable
    from a key that does not work, and costs an operator an afternoon either
    way.
    """

    def test_every_provider_credential_setting_appears_in_the_catalog(self):
        config = AppConfig()
        names = {name for name, _, _ in credential_catalog(config)}
        for expected in (
            config.reddit.client_id_credential,
            config.reddit.client_secret_credential,
            config.reddit.session_cookie_credential,
            config.x.bearer_credential,
            config.x.auth_token_credential,
            config.x.ct0_credential,
            config.polygon.api_key_credential,
            config.ai.anthropic_api_key_credential,
            config.ai.openai_api_key_credential,
            config.adanos.api_key_credential,
        ):
            assert expected in names, f"{expected} has no field on either credential screen"

    def test_optional_credentials_are_listed_before_they_are_configured(self):
        """Listing a field only once the credential exists is a trap: the
        operator needs the field in order to create it."""
        names = {name for name, _, _ in credential_catalog(AppConfig())}
        assert "polygon_api_key" in names
        assert "x_auth_token" in names

    def test_no_duplicate_credential_names(self):
        rows = credential_catalog(AppConfig())
        names = [name for name, _, _ in rows]
        assert len(names) == len(set(names))

    def test_every_entry_has_a_human_label_and_a_pipeline(self):
        for name, label, pipeline in credential_catalog(AppConfig()):
            assert label and label != name
            assert pipeline in {"sentiment", "stock_price"}

    def test_the_webapi_allowlist_is_the_same_object(self):
        """The write endpoint's allowlist must not be a copy that can drift."""
        from claudetrade.webapi.routers import system

        assert system._credential_catalog is credential_catalog
