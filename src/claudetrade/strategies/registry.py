"""Strategy registry.

Strategies self-register by decorating their class. The signal engine and the
backtester only ever refer to strategies by name, so adding one is a new file
plus a decorator -- no edits to the engine.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from claudetrade.config import AppConfig
from claudetrade.strategies.base import Strategy

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {}

T = TypeVar("T", bound=type[Strategy])


def register_strategy(cls: T) -> T:
    """Class decorator adding a strategy to the registry.

    Raises:
        ValueError: on a duplicate or missing name, which would otherwise make
            stored ``strategy`` fields ambiguous.
    """
    name = getattr(cls, "name", "")
    if not name or name == "unnamed":
        raise ValueError(f"{cls.__name__} must define a unique 'name'")
    existing = STRATEGY_REGISTRY.get(name)
    if existing is not None and existing is not cls:
        raise ValueError(f"strategy name '{name}' is already registered by {existing.__name__}")
    STRATEGY_REGISTRY[name] = cls
    return cls


def _load_builtin_strategies() -> None:
    """Import the built-in strategy modules so their decorators run."""
    from claudetrade.strategies import (  # noqa: F401
        a_sentiment_breakout,
        b_sentiment_pullback,
        c_capitulation_reversal,
        d_hype_failure_short,
        e_post_earnings_drift,
        f_volume_breakout,
    )


def available_strategies() -> list[str]:
    """Names of every registered strategy."""
    _load_builtin_strategies()
    return sorted(STRATEGY_REGISTRY)


def get_strategy(name: str, config: AppConfig) -> Strategy:
    """Instantiate one strategy by name.

    Raises:
        KeyError: when the name is not registered.
    """
    _load_builtin_strategies()
    try:
        cls = STRATEGY_REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown strategy '{name}'; available: {', '.join(sorted(STRATEGY_REGISTRY))}"
        ) from None
    return cls(config)


def build_strategies(config: AppConfig, names: list[str] | None = None) -> list[Strategy]:
    """Instantiate the configured strategy set (defaults to config)."""
    selected = names if names is not None else config.signals.enabled_strategies
    return [get_strategy(name, config) for name in selected]


def strategy_descriptions() -> dict[str, str]:
    """Name -> human description, for the UI's strategy picker."""
    _load_builtin_strategies()
    return {name: (cls.description or name) for name, cls in sorted(STRATEGY_REGISTRY.items())}


__all__ = [
    "STRATEGY_REGISTRY",
    "available_strategies",
    "build_strategies",
    "get_strategy",
    "register_strategy",
    "strategy_descriptions",
]


_Registrar = Callable[[type[Strategy]], type[Strategy]]
