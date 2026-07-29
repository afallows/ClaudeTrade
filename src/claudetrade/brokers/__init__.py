"""Execution boundary: the ``BrokerProvider`` ABC and its implementations.

Everything that transmits or manages an order lives here or beneath
``claudetrade.paper``. Strategies, signals and the backtester never import
from this package -- they produce ``Signal``/``TradePlan`` objects that get
handed to a ``BrokerProvider`` by whatever is driving the loop (the paper
runner today, a live executor later). See ADR-0007 Decision 4.
"""

from claudetrade.brokers.base import (
    Balances,
    BrokerOrder,
    BrokerOrderError,
    BrokerProvider,
    NotConfiguredError,
    OrderRequest,
    TradingHaltedError,
)

__all__ = [
    "Balances",
    "BrokerOrder",
    "BrokerOrderError",
    "BrokerProvider",
    "NotConfiguredError",
    "OrderRequest",
    "TradingHaltedError",
]
