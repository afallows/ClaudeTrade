"""Pluggable provider adapters.

Every external dependency -- market data, earnings, Reddit, X, AI models,
brokers -- sits behind a Protocol in ``base.py`` and is constructed through
``registry.py``. Swapping a vendor is a configuration change, not a rewrite.
"""

from claudetrade.providers.base import (
    AIProvider,
    EarningsProvider,
    MarketDataProvider,
    ProviderError,
    ProviderStatus,
    RateLimitError,
    SocialProvider,
)

__all__ = [
    "AIProvider",
    "EarningsProvider",
    "MarketDataProvider",
    "ProviderError",
    "ProviderStatus",
    "RateLimitError",
    "SocialProvider",
]
