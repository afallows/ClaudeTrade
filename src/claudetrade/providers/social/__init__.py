"""Social media provider adapters.

Exports:
    SyntheticSocialProvider: Offline deterministic test data generator.
    SyntheticRedditProvider: Synthetic Reddit posts.
    SyntheticXProvider: Synthetic X (Twitter) posts.
    RedditProvider: Real Reddit OAuth adapter.
    XProvider: Real X API v2 adapter.

All providers implement the SocialProvider protocol from providers.base.
Synthetic providers are always available offline; real providers require credentials.
"""

from __future__ import annotations

from claudetrade.providers.social.reddit import RedditProvider
from claudetrade.providers.social.synthetic import (
    SyntheticRedditProvider,
    SyntheticSocialProvider,
    SyntheticXProvider,
)
from claudetrade.providers.social.x_provider import XProvider

__all__ = [
    "RedditProvider",
    "SyntheticRedditProvider",
    "SyntheticSocialProvider",
    "SyntheticXProvider",
    "XProvider",
]
