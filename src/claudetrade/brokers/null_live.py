"""Stub live broker: proves the ABC has a second implementation shape.

``NullLiveBroker`` is not a real adapter -- it wires no venue, holds no
credentials, and every method raises ``NotConfiguredError``. It exists to
demonstrate two things ahead of a real Alpaca adapter:

1. The ``BrokerProvider`` surface is implementable by something that is *not*
   the paper broker, without speculative generality creeping into the ABC
   itself (ADR-0007 Decision 4's own stated risk).
2. The guard in ``brokers.base`` runs *before* an adapter is ever asked to do
   anything. With ``config.trading.mode`` left at its default (``"paper"``),
   every guarded call on this class raises ``TradingHaltedError`` and never
   reaches the ``NotConfiguredError`` below it -- the boundary refuses the
   call on its own, independent of whatever this adapter would have done.

A real Alpaca adapter replaces the bodies below with API calls; it does not
need to (and must not) touch the guard -- that stays inherited from
``BrokerProvider`` unchanged.
"""

from __future__ import annotations

from claudetrade.brokers.base import (
    Balances,
    BrokerOrder,
    BrokerProvider,
    NotConfiguredError,
    OrderRequest,
)
from claudetrade.config import AppConfig
from claudetrade.domain import Trade


class NullLiveBroker(BrokerProvider):
    """Live-broker shape with no venue behind it. Every call raises ``NotConfiguredError``."""

    name = "null_live"

    def __init__(self, config: AppConfig):
        self.config = config

    @property
    def is_paper(self) -> bool:
        return False

    @property
    def is_backtesting(self) -> bool:
        return False

    def _not_configured(self) -> NotConfiguredError:
        return NotConfiguredError(
            f"{self.name} has no venue or credentials configured; this stub exists to "
            "demonstrate the BrokerProvider shape, not to trade -- implement a real adapter "
            "(e.g. Alpaca) before using trading.mode='live'"
        )

    def _submit_order(self, request: OrderRequest) -> BrokerOrder:  # noqa: ARG002 -- ABC shape
        raise self._not_configured()

    def _cancel_order(self, order_id: str) -> BrokerOrder:  # noqa: ARG002 -- ABC shape
        raise self._not_configured()

    def _modify_order(
        self,
        order_id: str,  # noqa: ARG002 -- ABC shape
        *,
        stop_loss: float | None,  # noqa: ARG002 -- ABC shape
        targets: list[float] | None,  # noqa: ARG002 -- ABC shape
    ) -> BrokerOrder:
        raise self._not_configured()

    def get_balances(self) -> Balances:
        raise self._not_configured()

    def get_positions(self) -> list[Trade]:
        raise self._not_configured()

    def get_position(self, symbol: str) -> Trade | None:  # noqa: ARG002 -- ABC shape
        raise self._not_configured()

    def get_order(self, order_id: str) -> BrokerOrder | None:  # noqa: ARG002 -- ABC shape
        raise self._not_configured()

    def get_open_orders(self) -> list[BrokerOrder]:
        raise self._not_configured()
