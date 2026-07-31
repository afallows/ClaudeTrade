"""Local configuration and pipeline diagnostics endpoints.

Credential values cross the localhost API only on writes and are immediately
passed to the OS credential store. They are never persisted in application
configuration, returned in a response, or included in diagnostics.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, SecretStr

from claudetrade.config import AppConfig
from claudetrade.pipeline import Pipeline
from claudetrade.providers.base import (
    NotConfiguredError,
    ProviderError,
    RateLimitError,
    SourceBlockedError,
)
from claudetrade.secrets import delete_secret, get_secret, set_secret
from claudetrade.utils.timeutils import utc_now
from claudetrade.webapi.deps import get_config, get_pipeline
from claudetrade.webapi.refresh_state import RefreshState
from claudetrade.webapi.schemas import AIConfigOut, AIConfigUpdate

router = APIRouter(prefix="/api/system", tags=["system"])
log = logging.getLogger(__name__)


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
        (config.reddit.token_v2_credential, "Reddit token_v2 cookie (optional)", "sentiment"),
        (config.x.bearer_credential, "X bearer token", "sentiment"),
        (config.x.auth_token_credential, "X session auth token", "sentiment"),
        (config.x.ct0_credential, "X session CSRF token", "sentiment"),
        # Both AI provider credentials are always listed (not just the
        # currently-selected one) so the Configuration screen's AI Analysis
        # section can offer either Claude or ChatGPT without the operator
        # having to flip ``ai.provider`` first just to see the key field.
        (config.ai.anthropic_api_key_credential, "Anthropic (Claude) API key", "sentiment"),
        (config.ai.openai_api_key_credential, "OpenAI (ChatGPT) API key", "sentiment"),
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


@router.get("/ai-config")
def ai_config(config: AppConfig = Depends(get_config)) -> AIConfigOut:
    """Current AI-provider selection for the Configuration screen's
    "AI Analysis" section, plus each provider's own default model (rendered
    as the model field's placeholder) so the frontend never has to hardcode
    ``DEFAULT_MODEL`` from either provider module.
    """
    from claudetrade.providers.ai.anthropic_provider import DEFAULT_MODEL as ANTHROPIC_DEFAULT
    from claudetrade.providers.ai.openai_provider import DEFAULT_MODEL as OPENAI_DEFAULT

    return AIConfigOut(
        provider=config.ai.provider,
        model=config.ai.model,
        anthropic_default_model=ANTHROPIC_DEFAULT,
        openai_default_model=OPENAI_DEFAULT,
        anthropic_api_key_credential=config.ai.anthropic_api_key_credential,
        openai_api_key_credential=config.ai.openai_api_key_credential,
    )


@router.put("/ai-config")
def update_ai_config(
    body: AIConfigUpdate, config: AppConfig = Depends(get_config)
) -> dict[str, object]:
    """Switch the AI sentiment provider (None / Claude / ChatGPT) and,
    optionally, its model.

    **Scoped, honest persistence**: this updates the running server's
    in-memory ``AppConfig.ai`` immediately -- the Test button, the
    credential catalog's labels, ``/api/system/diagnostics``, and the next
    refresh/scan in *this* process all see the change right away. It does
    NOT rewrite ``config.toml`` on disk (this codebase has no in-app
    config-file writer for any field -- see
    ``ui.screens.settings.page_settings``'s module docstring, which
    documents the identical limitation for the Streamlit UI's other
    non-credential settings). A restart reverts to whatever ``config.toml``
    /environment variables specify. The response's ``persisted: false`` and
    ``note`` fields say this plainly, matching this app's "never claim a
    write path that isn't real" rule -- see ``docs/ai-setup.md`` for how to
    make the choice permanent via ``config.toml`` or
    ``CLAUDETRADE_AI__PROVIDER``/``CLAUDETRADE_AI__MODEL``.
    """
    config.ai.provider = body.provider
    config.ai.model = body.model
    return {
        "provider": config.ai.provider,
        "model": config.ai.model,
        "persisted": False,
        "note": (
            "Applied immediately for this running session. To make it "
            "permanent across restarts, add it to config.toml's [ai] table "
            "or set CLAUDETRADE_AI__PROVIDER / CLAUDETRADE_AI__MODEL."
        ),
    }


@router.post("/credentials/{source}/test")
def test_credential(source: str, config: AppConfig = Depends(get_config)) -> dict[str, object]:
    """On-demand live connectivity/classification test for one configured source.

    Makes exactly ONE small live outbound request (this is its whole
    purpose) -- never call this from an automated test against the real
    network; provider tests mock the transport/client instead, same as every
    other provider test in this repository.

    Implemented: ``reddit`` (a real, owner-observed 403-vs-200 discrepancy
    between tooling and a browser -- see ``docs/api-providers.md``), ``x``
    (the owner's way to validate the internal GraphQL endpoint constants in
    ``providers.social.x_provider`` live -- see that module's "expected
    maintenance" note), and ``ai`` (one minimal real classification call
    against the configured Claude/ChatGPT provider). Never echoes a
    credential value; the response carries only a pass/fail verdict, the
    mode/provider selected, and a short human-readable detail string.
    """
    if source == "reddit":
        return _test_reddit_connectivity(config)
    if source == "x":
        return _test_x_connectivity(config)
    if source == "ai":
        return _test_ai_connectivity(config)
    raise HTTPException(
        status_code=404, detail=f"no connectivity test is implemented for {source!r}"
    )


def _test_reddit_connectivity(config: AppConfig) -> dict[str, object]:
    from claudetrade.providers.social.reddit import RedditProvider

    # A deliberately cheap probe -- one subreddit, one page, a handful of
    # posts, and a tight timeout -- this is a connectivity check, not a real
    # refresh; it must return quickly regardless of the configured universe.
    probe_config = config.reddit.model_copy(
        update={
            "subreddits": [(config.reddit.subreddits or ["stocks"])[0]],
            "max_pages_per_subreddit": 1,
            "posts_per_subreddit": 3,
            "request_timeout_s": 15.0,
        }
    )

    try:
        provider = RedditProvider(probe_config)
    except NotConfiguredError as exc:
        return {"ok": False, "mode": None, "status_detail": f"not configured: {exc}"}

    mode = provider.mode
    if mode == "cookie_session":
        mode = f"{mode} ({'reddit_session + token_v2' if provider.has_token_v2 else 'reddit_session only'})"

    try:
        posts = provider.fetch_posts(since=utc_now() - dt.timedelta(hours=6), limit=3)
    except SourceBlockedError as exc:
        if provider.mode == "cookie_session":
            detail = (
                f"blocked (HTTP-level denial): {exc}. Cookie-session mode is confirmed to "
                "work from a non-browser client with a valid, current reddit_session cookie "
                "(owner-validated) -- a block here most likely means that cookie has expired "
                "or was mistyped; re-export a fresh reddit_session value from DevTools "
                "(Application -> Cookies -> https://www.reddit.com). token_v2 is optional and "
                "short-lived (hours, not weeks) -- its absence or expiry is not expected to "
                "block reddit_session-only requests. The password-grant OAuth path is the "
                "reliable alternative once your Reddit API app is approved; news RSS keeps "
                "sentiment flowing meanwhile."
            )
        else:
            detail = f"blocked: {exc}"
        return {"ok": False, "mode": mode, "status_detail": detail}
    except RateLimitError as exc:
        return {"ok": False, "mode": mode, "status_detail": f"rate limited: {exc}"}
    except ProviderError as exc:
        return {"ok": False, "mode": mode, "status_detail": f"provider error: {exc}"}
    except Exception as exc:  # never let a probe endpoint 500 the whole page
        return {"ok": False, "mode": mode, "status_detail": f"unexpected error: {exc}"}

    return {
        "ok": True,
        "mode": mode,
        "status_detail": f"fetched {len(posts)} post(s) from r/{probe_config.subreddits[0]}",
    }


def _test_x_connectivity(config: AppConfig) -> dict[str, object]:
    """Live probe for the configured X (Twitter) adapter.

    Follows ``_test_reddit_connectivity``'s exact pattern: instantiate the
    real ``XProvider``, make one deliberately cheap fetch, never echo a
    credential. This is also the owner's way to validate
    ``x_provider.py``'s internal GraphQL endpoint constants live -- session
    mode's ``_SEARCH_GRAPHQL_QUERY_ID`` is explicitly flagged in that module
    as expected-maintenance (x.com changes it without notice); a
    ``SourceBlockedError`` here is the first, fastest signal that a fresh
    browser capture is needed, well before a full scheduled refresh would
    surface the same failure.
    """
    from claudetrade.providers.social.x_provider import XProvider

    probe_config = config.x.model_copy(
        update={
            "query_terms": (config.x.query_terms or ["$AAPL"])[:1],
            "max_results_per_query": 3,
            "request_timeout_s": 15.0,
            "session_symbols": (config.x.session_symbols or ["AAPL"])[:1],
            "session_max_results_per_query": 3,
            "session_request_timeout_s": 15.0,
        }
    )

    try:
        provider = XProvider(probe_config)
    except NotConfiguredError as exc:
        return {"ok": False, "mode": None, "status_detail": f"not configured: {exc}"}

    mode = provider.mode

    try:
        posts = provider.fetch_posts(since=utc_now() - dt.timedelta(hours=6), limit=3)
    except SourceBlockedError as exc:
        if mode == "session":
            detail = (
                f"blocked (HTTP-level denial): {exc}. This usually means the "
                "auth_token/ct0 session cookies have expired or been logged out -- "
                "re-export fresh values from DevTools (Application -> Cookies -> "
                "https://x.com). It can also mean x.com changed its internal "
                "GraphQL endpoint again (expected maintenance -- see the "
                "constants block at the top of x_provider.py); if fresh cookies "
                "still fail, that constant needs a new browser capture."
            )
        else:
            detail = f"blocked: {exc}"
        return {"ok": False, "mode": mode, "status_detail": detail}
    except RateLimitError as exc:
        return {"ok": False, "mode": mode, "status_detail": f"rate limited: {exc}"}
    except ProviderError as exc:
        return {"ok": False, "mode": mode, "status_detail": f"provider error: {exc}"}
    except Exception as exc:  # never let a probe endpoint 500 the whole page
        return {"ok": False, "mode": mode, "status_detail": f"unexpected error: {exc}"}

    return {
        "ok": True,
        "mode": mode,
        "status_detail": f"fetched {len(posts)} post(s) (mode={mode})",
    }


#: Canned probe sentence for the AI connectivity test -- deliberately
#: mundane and unambiguous; the point is to prove the credential/model/SDK
#: path works end to end, not to exercise classification quality.
_AI_PROBE_TEXT = (
    "<<<TEXT\nShares broke out on strong volume after the earnings beat.\nTEXT>>>"
)


def _test_ai_connectivity(config: AppConfig) -> dict[str, object]:
    """Live probe for the configured AI (Claude/ChatGPT) sentiment provider.

    Follows the reddit/X probes' exact pattern: instantiate the real
    provider, make ONE minimal classification call against a canned
    sentence, never echo the API key. Reports which provider/model answered
    and whether the response parsed as valid structured output.
    """
    from claudetrade.providers.base import AIRequest

    if config.ai.provider == "none":
        return {
            "ok": False,
            "mode": None,
            "status_detail": "no AI provider selected (ai.provider = 'none')",
        }

    if config.ai.provider == "anthropic":
        try:
            from claudetrade.providers.ai.anthropic_provider import AnthropicProvider as _Cls
        except ImportError as exc:
            return {"ok": False, "mode": "anthropic", "status_detail": str(exc)}
    else:
        try:
            from claudetrade.providers.ai.openai_provider import OpenAIProvider as _Cls
        except ImportError as exc:
            return {"ok": False, "mode": "openai", "status_detail": str(exc)}

    probe_config = config.ai.model_copy(update={"request_timeout_s": 15.0})
    provider = _Cls(probe_config)  # never raises: see that class's __init__ docstring

    if not provider.has_credentials:
        return {
            "ok": False,
            "mode": config.ai.provider,
            "status_detail": f"credential '{config.ai.api_key_credential}' not configured",
        }

    request = AIRequest(
        task="sentiment",
        payload={"symbol": "TEST", "text": _AI_PROBE_TEXT},
        # Must match sentiment.ai_classifier.SCHEMA_NAME -- see the
        # SCHEMA_REGISTRY alias comment in providers.ai.schemas.
        schema_name="sentiment_classification_v1",
        max_output_tokens=200,
    )

    response = provider.complete(request)  # never raises: see that class's module docstring
    if not response.parsed_ok:
        detail = response.error or "response did not parse as valid structured output"
        return {"ok": False, "mode": config.ai.provider, "status_detail": detail}

    return {
        "ok": True,
        "mode": config.ai.provider,
        "status_detail": f"classified test sentence via {response.model}",
    }


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
                  config.ai.provider != "none", config.ai.provider != "none",
                  config.ai.api_key_credential),
    ]
    return {"pipelines": pipelines, "probe_note": "Network providers are tested during refresh; no secret-bearing request is made by this page."}


# --------------------------------------------------------------------------
# Background refresh (item 5: UI-first startup + background refresh)
# --------------------------------------------------------------------------


def _get_refresh_state(request: Request) -> RefreshState:
    return request.app.state.refresh_state


@router.post("/refresh")
def start_background_refresh(
    request: Request,
    pipeline: Pipeline = Depends(get_pipeline),
    config: AppConfig = Depends(get_config),
) -> dict[str, object]:
    """Start a data refresh on a background thread and return immediately.

    This is what ``scripts/setup.ps1``/``setup.bat`` call instead of running
    ``claudetrade refresh`` inline before the UI ever opens: the UI starts
    first, then triggers this endpoint against its own already-listening
    server, so the operator sees the app immediately and watches the refresh
    progress (``GET /api/system/refresh/status``) rather than staring at a
    blank terminal for 40+ minutes on a large universe.

    409 if a refresh is already running -- this process holds exactly one
    ``Pipeline``/database connection pool (see ``webapi.deps``'s module
    docstring), so a second concurrent refresh would race the first one's
    writes rather than run alongside it usefully.

    The CLI's ``claudetrade refresh`` is unchanged: it calls
    ``Pipeline.refresh`` directly, synchronously, with no progress callback,
    for scripted/scheduled use where blocking is exactly what is wanted.
    """
    state = _get_refresh_state(request)
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="a refresh is already running")
        state.running = True
        state.phase = "starting"
        state.symbols_done = 0
        state.symbols_total = 0
        state.started_at = utc_now()
        state.finished_at = None
        state.last_error = None

    def _run() -> None:
        end = utc_now().date()
        start = end - dt.timedelta(days=config.sentiment.lookback_days)
        try:
            pipeline.refresh(start=start, end=end, progress_callback=state.update_progress)
        except Exception as exc:
            with state.lock:
                state.last_error = str(exc)
            log.exception("background refresh failed")
        finally:
            with state.lock:
                state.running = False
                state.phase = "idle"
                state.finished_at = utc_now()

    threading.Thread(target=_run, name="claudetrade-background-refresh", daemon=True).start()
    return {"started": True}


@router.get("/refresh/status")
def refresh_status(request: Request) -> dict[str, object]:
    """Poll target for the setup script and the UI's progress banner."""
    return _get_refresh_state(request).snapshot()
