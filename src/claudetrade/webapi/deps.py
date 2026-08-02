"""FastAPI dependency providers.

Everything is read off ``request.app.state``, set once in
``claudetrade.webapi.app.create_app``. There is exactly one ``Pipeline`` (and
therefore one database connection pool) per running server process, matching
the single-operator, localhost-only deployment model documented in
``claudetrade.webapi.__main__``.
"""

from __future__ import annotations

from fastapi import Request

from claudetrade.config import AppConfig
from claudetrade.pipeline import Pipeline
from claudetrade.signals.engine import ScanResult


def get_pipeline(request: Request) -> Pipeline:
    return request.app.state.pipeline


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_last_scan(request: Request) -> ScanResult | None:
    """The ``ScanResult`` from the last ``POST /api/scan`` in this process, if any.

    Deliberately in-memory only, mirroring ``ScanResult.rejected`` never being
    persisted -- see ``claudetrade.signals.ledger``'s module docstring.
    """
    return getattr(request.app.state, "last_scan_result", None)


def set_last_scan(request: Request, result: ScanResult | None) -> None:
    request.app.state.last_scan_result = result


__all__ = ["get_config", "get_last_scan", "get_pipeline", "set_last_scan"]
