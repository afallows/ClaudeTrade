"""Trading strategies.

Each strategy is a self-contained rule set that inspects a point-in-time
``StrategyContext`` and either declines or returns a ``StrategyProposal``.
Strategies never touch the database, the network, or the clock -- everything
they may legally know is handed to them in the context. That restriction is
what makes look-ahead bias structurally hard rather than merely discouraged.
"""

from claudetrade.strategies.base import (
    Strategy,
    StrategyContext,
    StrategyProposal,
    StrategyRejection,
)
from claudetrade.strategies.registry import (
    STRATEGY_REGISTRY,
    available_strategies,
    build_strategies,
    get_strategy,
    register_strategy,
)

__all__ = [
    "STRATEGY_REGISTRY",
    "Strategy",
    "StrategyContext",
    "StrategyProposal",
    "StrategyRejection",
    "available_strategies",
    "build_strategies",
    "get_strategy",
    "register_strategy",
]
