"""Provider factory and registry.

Central configuration point for plugging in market, earnings, social and AI
data sources. Adapters are selected by name from the configuration; new
adapters are added by updating the name→class maps and registering them
with their respective factory function.
"""

from __future__ import annotations

import datetime as dt
import logging

from claudetrade.config import AppConfig
from claudetrade.domain import Bar, CorporateAction, SecurityInfo, SocialSource
from claudetrade.providers.base import (
    AIProvider,
    AIRequest,
    AIResponse,
    EarningsProvider,
    MarketDataProvider,
    NotConfiguredError,
    ProviderError,
    ProviderStatus,
    SocialProvider,
)
from claudetrade.providers.earnings.csv_provider import CSVEarningsProvider
from claudetrade.providers.earnings.synthetic import SyntheticEarningsProvider
from claudetrade.providers.market.csv_provider import CSVMarketProvider
from claudetrade.providers.market.stooq import StooqMarketProvider
from claudetrade.providers.market.synthetic import SyntheticMarketProvider
from claudetrade.providers.market.tipranks import TipRanksProvider
from claudetrade.providers.market.yahoo import YahooMarketProvider

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Provider name → class maps
# --------------------------------------------------------------------------

#: Market data adapters. Add new providers here.
_MARKET_PROVIDERS: dict[str, type[MarketDataProvider]] = {
    "synthetic": SyntheticMarketProvider,
    "csv": CSVMarketProvider,
    "stooq": StooqMarketProvider,
    # Yahoo's chart endpoint is a bars fallback only -- it no longer exposes
    # get_market_caps (the quote API that backed it required cookie+crumb
    # auth and was removed outright; see providers.market.yahoo).
    "yahoo": YahooMarketProvider,
    # Primary source (ADR-0008 Decision 1 amendment): real market caps,
    # reference data and earnings from one unauthenticated widget call per
    # symbol. Its own daily-bars capability is deliberately last-resort only
    # (TipRanksProvider.bars_last_resort) -- see
    # FallbackMarketProvider.get_daily_bars for the cascade mechanism this
    # relies on.
    "tipranks": TipRanksProvider,
}

#: Earnings adapters. Add new providers here.
_EARNINGS_PROVIDERS: dict[str, type[EarningsProvider]] = {
    "synthetic": SyntheticEarningsProvider,
    "csv": CSVEarningsProvider,
    # Same TipRanksProvider instance-shape as the market registry above --
    # EarningsProvider is a structural Protocol, so this class satisfies it
    # without a second inheritance declaration. Registered here as the
    # DEFAULT earnings provider (see EarningsConfig.provider); synthetic
    # remains fully selectable for offline/demo use.
    "tipranks": TipRanksProvider,
}


# --------------------------------------------------------------------------
# Fallback market provider: tries primary, then each fallback in order
# --------------------------------------------------------------------------


class FallbackMarketProvider:
    """Cascading market data adapter with degraded capability.

    Tries the primary provider, then each fallback in the config's fallback
    list. Logs each degradation event. For any query symbol, missing data
    from the primary is filled from fallbacks, so the caller gets the best
    available data per symbol rather than an all-or-nothing failure.

    This is the provider most callers receive; they interact with a single
    unified interface while the fallback logic runs invisibly.
    """

    name = "fallback"

    def __init__(
        self,
        primary: MarketDataProvider,
        fallbacks: list[MarketDataProvider],
    ) -> None:
        self.primary = primary
        self.fallbacks = fallbacks

    def status(self) -> ProviderStatus:
        """Status of the primary; fallbacks are listed in the message."""
        primary_status = self.primary.status()
        fallback_names = [f.name for f in self.fallbacks] if self.fallbacks else []
        msg = primary_status.message
        if fallback_names:
            msg += f" (fallbacks: {', '.join(fallback_names)})"
        return ProviderStatus(
            name=self.name,
            kind="market",
            available=primary_status.available,
            configured=primary_status.configured,
            message=msg,
            supports_point_in_time=primary_status.supports_point_in_time,
            supports_delisted=primary_status.supports_delisted,
            rate_limit_per_minute=primary_status.rate_limit_per_minute,
            licence_note=(
                f"Primary: {primary_status.licence_note} "
                f"Fallbacks: {'; '.join(f.status().licence_note for f in self.fallbacks)}"
                if self.fallbacks
                else primary_status.licence_note
            ),
        )

    def _bars_cascade(self) -> list[MarketDataProvider]:
        """Ordered providers to try for ``get_daily_bars`` specifically.

        Normally this is just ``[primary, *fallbacks]``, the same order every
        other capability (``get_security_info``, and ``DataIngestor.
        enrich_market_caps``'s own ``.primary``/``.fallbacks`` walk) uses.
        Bars are the one exception: a provider whose ``bars_last_resort``
        class/instance attribute is ``True`` (currently only
        ``providers.market.tipranks.TipRanksProvider``, whose bars are
        close-only and exist purely as a fallback of last resort -- see that
        module's docstring) is moved to the very end of the cascade *for this
        method only*, even when it is configured as the primary provider.
        This is a plain ``getattr`` check (default ``False``), not a
        ``Protocol`` requirement or a ``status().capabilities`` lookup, so
        every pre-existing provider (synthetic/csv/stooq/yahoo) is completely
        unaffected -- they simply lack the attribute and sort first, exactly
        as before this mechanism existed.
        """
        chain = [self.primary, *self.fallbacks]
        deferred = [p for p in chain if getattr(p, "bars_last_resort", False)]
        normal = [p for p in chain if not getattr(p, "bars_last_resort", False)]
        return normal + deferred

    def get_daily_bars(
        self,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        *,
        adjusted: bool = True,
    ) -> dict[str, list[Bar]]:
        """Get daily bars, filling missing symbols from the bars cascade.

        See ``_bars_cascade`` for why this can differ from simple
        primary-then-fallbacks order.
        """
        out: dict[str, list[Bar]] = {}
        unfilled: set[str] = set(symbols)

        for provider in self._bars_cascade():
            if not unfilled:
                break
            try:
                result = provider.get_daily_bars(list(unfilled), start, end, adjusted=adjusted)
            except ProviderError as exc:
                log.warning(
                    "market provider %s failed fetching bars for %d symbols: %s; "
                    "trying the next source in the cascade",
                    provider.name, len(unfilled), exc,
                )
                continue

            filled_now: set[str] = set()
            for symbol, bars in result.items():
                if bars and symbol not in out:
                    out[symbol] = bars
                    filled_now.add(symbol)
            if filled_now:
                log.info("filled %s from %s", filled_now, provider.name)
            unfilled -= filled_now

        # Return all requested symbols with empty lists for anything unfilled.
        for symbol in symbols:
            out.setdefault(symbol, [])

        return out

    def get_intraday_bars(
        self,
        symbols: list[str],
        start: dt.datetime,
        end: dt.datetime,
        *,
        interval_minutes: int = 5,
    ) -> dict[str, list[Bar]]:
        """Get intraday bars (not cascaded; primary only)."""
        return self.primary.get_intraday_bars(
            symbols, start, end, interval_minutes=interval_minutes
        )

    def get_security_info(self, symbols: list[str]) -> dict[str, SecurityInfo]:
        """Get reference data, cascading through fallbacks."""
        out: dict[str, SecurityInfo] = {}
        unfilled = set(symbols)

        try:
            primary_result = self.primary.get_security_info(list(unfilled))
            for symbol, info in primary_result.items():
                if info and info.name:  # Treat minimal stubs as unfilled.
                    out[symbol] = info
                    unfilled.discard(symbol)
        except ProviderError:
            pass

        for fallback in self.fallbacks:
            if not unfilled:
                break
            try:
                fallback_result = fallback.get_security_info(list(unfilled))
                for symbol, info in fallback_result.items():
                    if symbol not in out and info and info.name:
                        out[symbol] = info
                        unfilled.discard(symbol)
            except ProviderError:
                continue

        return out

    def get_corporate_actions(
        self, symbols: list[str], start: dt.date, end: dt.date
    ) -> dict[str, list[CorporateAction]]:
        """Get corporate actions (not cascaded; primary only)."""
        return self.primary.get_corporate_actions(symbols, start, end)

    def list_universe(self, *, as_of: dt.date | None = None) -> list[SecurityInfo]:
        """Get universe (primary only)."""
        return self.primary.list_universe(as_of=as_of)


# --------------------------------------------------------------------------
# Factory functions
# --------------------------------------------------------------------------


def get_market_provider(config: AppConfig) -> MarketDataProvider:
    """Get the market data provider with fallbacks.

    Args:
        config: Application configuration.

    Returns:
        A MarketDataProvider (usually FallbackMarketProvider wrapping the primary).

    Raises:
        ProviderError: if the primary provider cannot be instantiated.
    """
    primary_name = config.market_data.provider
    if primary_name not in _MARKET_PROVIDERS:
        raise ProviderError(
            f"unknown market provider: {primary_name}. "
            f"Available: {list(_MARKET_PROVIDERS.keys())}",
            provider="registry",
        )

    # Instantiate primary.
    primary_class = _MARKET_PROVIDERS[primary_name]
    try:
        primary = _instantiate_market_provider(primary_name, primary_class, config)
    except Exception as exc:
        raise ProviderError(
            f"failed to instantiate {primary_name}: {exc}",
            provider="registry",
        ) from exc

    log.info("loaded primary market provider: %s", primary.name)

    # Instantiate fallbacks.
    fallbacks: list[MarketDataProvider] = []
    for fallback_name in config.market_data.fallbacks:
        if fallback_name not in _MARKET_PROVIDERS:
            log.warning(
                "unknown fallback market provider: %s; skipping", fallback_name
            )
            continue
        try:
            fallback_class = _MARKET_PROVIDERS[fallback_name]
            fallback = _instantiate_market_provider(fallback_name, fallback_class, config)
            fallbacks.append(fallback)
            log.info("loaded fallback market provider: %s", fallback.name)
        except Exception as exc:
            log.warning(
                "failed to instantiate fallback %s: %s; skipping",
                fallback_name, exc
            )

    return FallbackMarketProvider(primary, fallbacks)


def _instantiate_market_provider(
    name: str, provider_class: type[MarketDataProvider], config: AppConfig
) -> MarketDataProvider:
    """Construct one named market-data adapter with its own constructor shape.

    Pulled out of ``get_market_provider`` since it is used identically for
    both the primary and every fallback slot.
    """
    if name == "csv":
        return provider_class(csv_dir=config.market_data.csv_dir)  # type: ignore[call-arg]
    if name == "tipranks":
        return provider_class(  # type: ignore[call-arg]
            config=config.tipranks, cache_dir=config.paths.resolve("cache_dir")
        )
    # stooq, yahoo, synthetic all take the shared MarketDataConfig.
    return provider_class(config=config.market_data)  # type: ignore[call-arg]


def get_earnings_provider(config: AppConfig) -> EarningsProvider:
    """Get the earnings provider.

    Args:
        config: Application configuration.

    Returns:
        An EarningsProvider.

    Raises:
        ProviderError: if the provider cannot be instantiated.
    """
    primary_name = config.earnings.provider
    if primary_name not in _EARNINGS_PROVIDERS:
        raise ProviderError(
            f"unknown earnings provider: {primary_name}. "
            f"Available: {list(_EARNINGS_PROVIDERS.keys())}",
            provider="registry",
        )

    primary_class = _EARNINGS_PROVIDERS[primary_name]
    try:
        if primary_name == "csv":
            primary = primary_class(csv_path=config.earnings.csv_path)
        elif primary_name == "tipranks":
            primary = primary_class(
                config=config.tipranks, cache_dir=config.paths.resolve("cache_dir")
            )
        else:  # synthetic
            primary = primary_class()
    except Exception as exc:
        raise ProviderError(
            f"failed to instantiate {primary_name}: {exc}",
            provider="registry",
        ) from exc

    log.info("loaded earnings provider: %s", primary.name)
    return primary


def _build_reddit_provider(config: AppConfig) -> SocialProvider | None:
    """Instantiate the configured Reddit adapter, or ``None`` when unavailable."""
    if config.reddit.provider == "synthetic":
        from claudetrade.providers.social.synthetic import SyntheticRedditProvider

        # Seeded from the backtest seed so a run is reproducible end to end.
        return SyntheticRedditProvider(seed=config.backtest.random_seed)

    from claudetrade.providers.social.reddit import RedditProvider

    return RedditProvider(config.reddit)


def _build_x_provider(config: AppConfig) -> SocialProvider | None:
    """Instantiate the configured X adapter, or ``None`` when unavailable."""
    if config.x.provider == "synthetic":
        from claudetrade.providers.social.synthetic import SyntheticXProvider

        # Offset the seed so the two synthetic sources do not emit identical
        # corpora, which would make source-concentration metrics meaningless.
        return SyntheticXProvider(seed=config.backtest.random_seed + 1)

    from claudetrade.providers.social.x_provider import XProvider

    return XProvider(config.x)


def _build_stocktwits_provider(config: AppConfig) -> SocialProvider | None:
    """Instantiate the Stocktwits adapter, or ``None`` when unavailable.

    No credentials are required (keyless public reads), so there is no
    synthetic/live selector like Reddit/X/News have -- ``enabled`` alone
    gates this source, consistent with its ADR-0008 Decision 1 "official
    API, opt-in due to rate budget" posture.
    """
    from claudetrade.providers.social.stocktwits import StocktwitsProvider

    return StocktwitsProvider(config.stocktwits)


def _build_news_provider(config: AppConfig) -> SocialProvider | None:
    """Instantiate the configured news adapter, or ``None`` when unavailable.

    Unlike Reddit/X, ``news_rss`` (not ``synthetic``) is the default: RSS
    needs no credentials and no paid tier, so there is nothing to gate behind
    an opt-in the way Reddit's OAuth app or X's paid API tier are gated.
    """
    if config.news.provider == "synthetic":
        from claudetrade.providers.social.synthetic import SyntheticSocialProvider

        # Offset the seed so this doesn't emit an identical corpus to the
        # other synthetic sources, which would make source-concentration
        # metrics meaningless.
        return SyntheticSocialProvider(
            source=SocialSource.NEWS,
            seed=config.backtest.random_seed + 2,
            base_author_salt="news_synthetic",
        )

    from claudetrade.providers.social.news_rss import NewsRssProvider

    return NewsRssProvider(config.news)


def get_social_providers(config: AppConfig) -> list[SocialProvider]:
    """Construct the enabled social providers.

    The ``provider`` field on each source selects the adapter, so the offline
    synthetic generator is reachable without credentials -- that is the default
    and is what lets the whole application be exercised and tested with no API
    keys.

    A source that is disabled, whose module is missing, or whose credentials do
    not resolve is skipped cleanly. The pipeline continues with the remaining
    sources rather than failing, which is the documented reduced-capability
    behaviour.
    """
    providers: list[SocialProvider] = []

    for enabled, name, builder in (
        (config.reddit.enabled, "reddit", _build_reddit_provider),
        (config.x.enabled, "x", _build_x_provider),
        (config.stocktwits.enabled, "stocktwits", _build_stocktwits_provider),
        (config.news.enabled, "news", _build_news_provider),
    ):
        if not enabled:
            continue
        try:
            provider = builder(config)
        except NotConfiguredError as exc:
            # The expected path when a live adapter has no credentials.
            log.info("social source '%s' disabled: %s", name, exc)
            continue
        except ImportError:
            log.debug("social provider module for '%s' is not available; skipping", name)
            continue
        except Exception as exc:
            log.warning("failed to initialise social provider '%s': %s", name, exc)
            continue
        if provider is not None:
            providers.append(provider)
            log.info("loaded social provider: %s", getattr(provider, "name", name))

    return providers


def get_ai_provider(config: AppConfig) -> AIProvider:
    """Get the AI provider (OpenAI, Anthropic, or a null stub).

    If the configured provider's module is not available or credentials are
    missing, returns a minimal null provider that satisfies the AIProvider
    protocol with parsed_ok=False and fallback_used="rules".

    Args:
        config: Application configuration.

    Returns:
        An AIProvider instance.
    """
    if config.ai.provider == "null":
        return _NullAIProvider()

    if config.ai.provider == "openai":
        try:
            from claudetrade.providers.ai.openai_provider import (
                OpenAIProvider,
            )

            try:
                return OpenAIProvider(config.ai)
            except Exception as exc:
                log.warning("failed to initialize openai provider: %s; using null", exc)
                return _NullAIProvider()
        except ImportError:
            log.debug("openai provider module not available; using null")
            return _NullAIProvider()

    if config.ai.provider == "anthropic":
        try:
            from claudetrade.providers.ai.anthropic_provider import (
                AnthropicProvider,
            )

            try:
                return AnthropicProvider(config.ai)
            except Exception as exc:
                log.warning(
                    "failed to initialize anthropic provider: %s; using null", exc
                )
                return _NullAIProvider()
        except ImportError:
            log.debug("anthropic provider module not available; using null")
            return _NullAIProvider()

    log.warning("unknown ai provider: %s; using null", config.ai.provider)
    return _NullAIProvider()


def provider_status_report(config: AppConfig) -> list[ProviderStatus]:
    """Generate a status report for the dashboard.

    Args:
        config: Application configuration.

    Returns:
        List of ProviderStatus records for market, earnings, social, and AI.
    """
    statuses: list[ProviderStatus] = []

    # Market provider.
    try:
        market = get_market_provider(config)
        statuses.append(market.status())
    except Exception as exc:
        statuses.append(
            ProviderStatus(
                name="market",
                kind="market",
                available=False,
                configured=False,
                message=str(exc),
            )
        )

    # Earnings provider.
    try:
        earnings = get_earnings_provider(config)
        statuses.append(earnings.status())
    except Exception as exc:
        statuses.append(
            ProviderStatus(
                name="earnings",
                kind="earnings",
                available=False,
                configured=False,
                message=str(exc),
            )
        )

    # Social providers.
    for provider in get_social_providers(config):
        statuses.append(provider.status())

    # AI provider.
    ai = get_ai_provider(config)
    statuses.append(ai.status())

    return statuses


# --------------------------------------------------------------------------
# Null AI provider (fallback)
# --------------------------------------------------------------------------


class _NullAIProvider:
    """Minimal null AI provider returned when the real adapter isn't available.

    Satisfies the AIProvider protocol by returning parsed_ok=False responses
    so callers fall back to deterministic rule-based fallbacks.
    """

    name = "null"
    model = "none"

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            kind="ai",
            available=False,
            configured=False,
            message="AI provider disabled; using rule-based fallback",
            licence_note="No external AI calls are made.",
            capabilities={"fallback_only": True},
        )

    def complete(self, request: AIRequest) -> AIResponse:
        """Always return unparsed responses so caller uses fallback."""
        return AIResponse(
            task=request.task,
            provider=self.name,
            model=self.model,
            prompt_version=request.prompt_version,
            created_at=dt.datetime.now(tz=dt.UTC),
            data=None,
            parsed_ok=False,
            fallback_used="rules",
        )

    def complete_batch(self, requests: list[AIRequest]) -> list[AIResponse]:
        return [self.complete(req) for req in requests]
