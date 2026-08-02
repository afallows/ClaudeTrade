"""Adapter seam for a hosted/paid sentiment or news aggregator.

A real hosted aggregator now exists: see ``providers.social.adanos``.

**This module is a stub.** It exists to give the application a documented
place to plug in a commercial sentiment/news vendor (e.g. one covering more
outlets than the free RSS/Atom list in ``news_rss.py``, or offering deeper
historical coverage) without inventing a fake implementation to fill the gap
in the meantime. ``HostedSentimentProvider`` never returns data: its
constructor always raises ``NotConfiguredError``, whether or not it is
configured, because there is no vendor integration behind it yet. This keeps
the provider protocol satisfiable (so ``get_social_providers`` can reference
the class without special-casing it) while never pretending to work.

A real implementation replacing this stub MUST provide, at minimum:

* **Historical depth for backtests.** State plainly, in ``status()``'s
  ``licence_note``, how far back the vendor's API can serve data and whether
  that window is fixed (e.g. "90 days rolling") or grows over time. ADR-0007
  rejected adopting ``openbb-adanos`` as a first-class source for exactly
  this reason -- a hosted aggregator capped at 90 days cannot feed an honest
  multi-year backtest, only paper/live operation going forward. If the vendor
  has the same cap, the backtester must be told (``supports_point_in_time``
  and a ``licence_note`` caveat), not silently starved of history.
* **Per-post vs pre-aggregated data.** Declare which shape the vendor
  actually returns. If it is individual posts/articles, they must be run
  through the same ``sanitize_social_text``/``injection_risk_score``/
  ``pseudonymise`` pipeline every other adapter in this package uses before
  anything is stored -- untrusted third-party text is untrusted regardless of
  which vendor relayed it. If the vendor instead returns *pre-aggregated*
  scores (a single "sentiment: 0.62" per symbol/day with no underlying
  posts), that must NOT be forced into synthetic ``SocialPost`` records to
  satisfy this protocol's shape -- fabricating fake per-post rows to feed
  ``SentimentAggregator`` would corrupt post-count/unique-author-based
  confidence and manipulation heuristics with numbers that were never real
  posts. A pre-aggregated vendor needs its own ingestion path directly into
  ``SymbolSentiment``-shaped data, which is out of scope for the
  ``SocialProvider`` protocol this stub implements.
* **Licensing caveats per ADR-0007 Decision 5.** Record, in ``status()``,
  whatever the vendor's terms say about: redistribution of scored output,
  whether raw source text may be persisted at all (some vendors license only
  the derived score, not the underlying article/post), and any restriction on
  using the data to drive automated trading versus research/display only.
  These are exactly the caveats this project already surfaces for Stooq
  (personal-research-only) and Reddit/X (their respective API terms) in
  ``docs/api-providers.md`` -- a hosted vendor needs the same treatment
  before it is wired in for real.

None of the above is implemented here. Implementing it means: instantiate a
rate-limited HTTP client against ``config.hosted_base_url`` using the
credential named by ``config.hosted_credential`` (resolved via
``claudetrade.secrets.get_secret``, never inlined), replace the final
``raise NotConfiguredError`` in ``__init__`` with the real setup, and
implement ``fetch_posts``/``status`` against the vendor's actual response
shape -- mirroring ``reddit.py`` and ``news_rss.py``'s idiom for
sanitisation, author hashing, and clean degradation on missing credentials or
rate limits.
"""

from __future__ import annotations

import datetime as dt

from claudetrade.config import NewsConfig
from claudetrade.domain import SocialPost, SocialSource
from claudetrade.providers.base import NotConfiguredError, ProviderStatus

__all__ = ["HostedSentimentProvider"]


class HostedSentimentProvider:
    """STUB seam for a paid, hosted sentiment/news aggregator.

    See the module docstring for what a real implementation must add before
    this stops raising. Configuration lives on ``NewsConfig`` (``base_url``,
    ``credential`` name, and a feature flag): a hosted vendor is treated as an
    extension of the news source family, not a separate config section.

    The constructor requires all three of ``config.hosted_base_url``,
    ``config.hosted_credential`` and ``config.hosted_enabled`` before it will
    even consider proceeding -- and then raises anyway, because "considering
    proceeding" is as far as this stub goes. This mirrors every other
    adapter's clean-degradation contract: a caller iterating providers via
    ``get_social_providers`` catches ``NotConfiguredError`` and simply does
    not get this source, the same as an unconfigured Reddit/X adapter today.
    """

    name: str = "hosted_sentiment"
    source: SocialSource = SocialSource.NEWS

    def __init__(self, config: NewsConfig):
        """Validate configuration, then refuse to proceed.

        Args:
            config: ``NewsConfig``, whose ``hosted_*`` fields describe the
                (currently unimplemented) hosted vendor seam.

        Raises:
            NotConfiguredError: always. With a "missing configuration"
                message when ``hosted_base_url``/``hosted_credential`` are
                unset or ``hosted_enabled`` is false (the ordinary, expected
                disabled path); with a "stub, not implemented" message when
                all three are set (an operator who tried to turn this on
                still gets a clean, honest refusal rather than fabricated
                data).
        """
        self.config = config
        if not (config.hosted_enabled and config.hosted_base_url and config.hosted_credential):
            raise NotConfiguredError(
                "hosted sentiment provider is not configured: requires "
                "news.hosted_base_url, news.hosted_credential and "
                "news.hosted_enabled=true to all be set",
                provider="hosted_sentiment",
            )
        # All three are set, i.e. an operator explicitly opted in. This is
        # still a stub with no vendor integration behind it -- see the module
        # docstring for what must be built before this line is reached
        # without raising.
        raise NotConfiguredError(
            "HostedSentimentProvider is an unimplemented adapter seam; no "
            "hosted aggregator is wired up. See providers/social/hosted_api.py "
            "for what a real implementation must provide before this stub can "
            "be replaced.",
            provider="hosted_sentiment",
        )

    def status(self) -> ProviderStatus:  # pragma: no cover - unreachable while stubbed
        """Status is unreachable while the constructor always raises."""
        return ProviderStatus(
            name=self.name,
            kind="social",
            available=False,
            configured=False,
            message="stub: no hosted sentiment aggregator implemented",
            licence_note="No vendor is wired up; see class docstring for requirements.",
        )

    def fetch_posts(
        self,
        *,
        since: dt.datetime,  # noqa: ARG002
        until: dt.datetime | None = None,  # noqa: ARG002
        symbols: list[str] | None = None,  # noqa: ARG002
        limit: int | None = None,  # noqa: ARG002
    ) -> list[SocialPost]:  # pragma: no cover - unreachable while stubbed
        """Unreachable while the constructor always raises."""
        raise NotConfiguredError(
            "HostedSentimentProvider is a stub with no vendor integration",
            provider="hosted_sentiment",
        )
