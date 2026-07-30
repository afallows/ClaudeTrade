"""FastAPI layer over the existing pipeline/ledger/backtest/paper engine.

Serves the ADR-0008 Decision 2 React SPA as typed JSON. This package never
reimplements domain logic: every endpoint imports and calls the existing
``claudetrade.pipeline.Pipeline``, ``claudetrade.signals.ledger.SignalLedger``,
``claudetrade.paper.portfolio.PaperPortfolio`` and ``claudetrade.paper.broker.
PaperBroker`` -- the same objects the CLI and the Streamlit UI already use --
and translates their results to pydantic response models.

Run it with ``python -m claudetrade.webapi``; see that module's docstring for
the security model (localhost-only, no auth) and startup sequence.
"""

from __future__ import annotations
