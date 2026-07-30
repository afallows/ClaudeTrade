"""Local configuration and pipeline diagnostics endpoints.

Credential values cross the localhost API only on writes and are immediately
passed to the OS credential store. They are never persisted in application
configuration, returned in a response, or included in diagnostics.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, SecretStr

from claudetrade.config import AppConfig
from claudetrade.secrets import delete_secret, get_secret, set_secret
from claudetrade.webapi.deps import get_config

router = APIRouter(prefix="/api/system", tags=["system"])


class CredentialWrite(BaseModel):
    value: SecretStr = Field(min_length=1, max_length=16_384)


def _credential_catalog(config: AppConfig) -> list[tuple[str, str, str]]:
    """Allowlisted credentials exposed in the UI (name, label, pipeline)."""
    items = [
        (config.reddit.client_id_credential, "Reddit client ID", "sentiment"),
        (config.reddit.client_secret_credential, "Reddit client secret", "sentiment"),
        (config.reddit.username_credential, "Reddit username", "sentiment"),
        (config.reddit.password_credential, "Reddit password", "sentiment"),
        (config.reddit.session_cookie_credential, "Reddit session cookie", "sentiment"),
        (config.x.bearer_credential, "X bearer token", "sentiment"),
        (config.x.auth_token_credential, "X session auth token", "sentiment"),
        (config.x.ct0_credential, "X session CSRF token", "sentiment"),
        (config.ai.api_key_credential, f"{config.ai.provider.title()} API key", "sentiment"),
    ]
    if config.market_data.credential:
        items.append((config.market_data.credential, "Market data API key", "stock_price"))
    if config.earnings.credential:
        items.append((config.earnings.credential, "Earnings API key", "stock_price"))
    if config.news.hosted_credential:
        items.append((config.news.hosted_credential, "Hosted sentiment API key", "sentiment"))
    # Config may intentionally reuse a key; show/store it only once.
    return list({item[0]: item for item in items}.values())


def _credential(config: AppConfig, name: str) -> tuple[str, str, str]:
    try:
        return next(item for item in _credential_catalog(config) if item[0] == name)
    except StopIteration as exc:
        raise HTTPException(status_code=404, detail="unknown credential") from exc


@router.get("/credentials")
def credentials(config: AppConfig = Depends(get_config)) -> dict[str, object]:
    result = []
    for name, label, pipeline in _credential_catalog(config):
        secret = get_secret(name)
        result.append({
            "name": name, "label": label, "pipeline": pipeline,
            "configured": secret is not None,
            "source": secret.source if secret else None,
            "masked": secret.masked() if secret else None,
        })
    return {"credentials": result, "storage": "OS credential store or environment"}


@router.put("/credentials/{name}")
def write_credential(name: str, body: CredentialWrite, config: AppConfig = Depends(get_config)) -> dict[str, object]:
    _credential(config, name)
    try:
        backend = set_secret(name, body.value.get_secret_value())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"name": name, "configured": True, "source": backend}


@router.delete("/credentials/{name}", status_code=status.HTTP_204_NO_CONTENT)
def remove_credential(name: str, config: AppConfig = Depends(get_config)) -> Response:
    _credential(config, name)
    existing = get_secret(name)
    if existing and existing.source == "environment":
        raise HTTPException(status_code=409, detail="environment credentials must be removed from the environment")
    delete_secret(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _pipeline(name: str, kind: Literal["sentiment", "stock_price"], provider: str,
              enabled: bool, needs_credential: bool, credential: str | None = None) -> dict[str, object]:
    configured = enabled and (not needs_credential or bool(credential and get_secret(credential)))
    # This endpoint deliberately performs no vendor requests. "reachable" means
    # the selected adapter can be invoked locally; runtime vendor reachability
    # is established by refresh and represented as unknown until then.
    local = provider in {"synthetic", "csv"}
    return {
        "name": name, "kind": kind, "provider": provider,
        "status": "not_configured" if not configured else ("reachable" if local else "configured"),
        "configured": configured, "reachable": True if configured and local else None,
        "detail": "Ready (local provider)" if configured and local else (
            "Configured; network reachability is checked during refresh" if configured else
            ("Credential required" if enabled else "Disabled in configuration")
        ),
    }


_REDDIT_MODE_LABELS = {
    "password": "OAuth password grant",
    "cookie_session": "Cookie session",
    "client_credentials": "OAuth client-credentials grant",
    "public_json": "Public JSON fallback",
}


def _reddit_pipeline(config: AppConfig) -> dict[str, object]:
    """Reddit diagnostics, mirroring ``RedditProvider``'s own mode-selection
    order (password grant -> cookie session -> client-credentials ->
    opt-in public-JSON) without making any vendor request, so the reported
    mode always matches what a real refresh would pick.
    """
    reddit = config.reddit
    if reddit.provider != "reddit":
        # Not pointed at the live adapter at all (e.g. still "synthetic").
        return _pipeline("Reddit", "sentiment", reddit.provider, reddit.enabled,
                          False)

    has_client_creds = bool(
        get_secret(reddit.client_id_credential) and get_secret(reddit.client_secret_credential)
    )
    has_user_creds = bool(
        get_secret(reddit.username_credential) and get_secret(reddit.password_credential)
    )
    has_cookie = get_secret(reddit.session_cookie_credential) is not None

    if has_client_creds and has_user_creds:
        mode: str | None = "password"
    elif has_cookie:
        mode = "cookie_session"
    elif has_client_creds:
        mode = "client_credentials"
    elif reddit.public_json_fallback:
        mode = "public_json"
    else:
        mode = None

    configured = reddit.enabled and mode is not None
    if not reddit.enabled:
        detail = "Disabled in configuration"
    elif mode is None:
        detail = (
            "Credential required (client ID/secret, username/password, or a "
            "session cookie)"
        )
    else:
        detail = (
            f"{_REDDIT_MODE_LABELS[mode]} configured; network reachability is "
            "checked during refresh"
        )
    return {
        "name": "Reddit", "kind": "sentiment", "provider": reddit.provider,
        "status": "not_configured" if not configured else "configured",
        "configured": configured, "reachable": None,
        "detail": detail,
    }


@router.get("/diagnostics")
def diagnostics(config: AppConfig = Depends(get_config)) -> dict[str, object]:
    pipelines = [
        _pipeline("Market prices", "stock_price", config.market_data.provider, True,
                  bool(config.market_data.credential), config.market_data.credential),
        _reddit_pipeline(config),
        _pipeline("X", "sentiment", config.x.provider, config.x.enabled,
                  config.x.provider != "synthetic", config.x.bearer_credential),
        _pipeline("Stocktwits", "sentiment", config.stocktwits.provider,
                  config.stocktwits.enabled, False),
        _pipeline("News RSS", "sentiment", config.news.provider,
                  config.news.enabled, config.news.hosted_enabled,
                  config.news.hosted_credential),
        _pipeline("AI classifier", "sentiment", config.ai.provider,
                  config.ai.provider != "null", config.ai.provider != "null",
                  config.ai.api_key_credential),
    ]
    return {"pipelines": pipelines, "probe_note": "Network providers are tested during refresh; no secret-bearing request is made by this page."}
