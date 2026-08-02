"""``Pipeline.collect_social`` and ``claudetrade sentiment collect``.

The one property worth guarding above all others here is what a collection
does *not* do. The hourly loop exists because social data is cheap and
unrecoverable; a full refresh is expensive and fully recoverable. If a
collection ever reached the market pass, the hourly cadence would burn the
market provider's rate budget re-fetching bars that had not moved, and the
feature would have to be turned off -- which loses the baseline it exists to
build. So: no market provider, no earnings provider, no universe network call.

Everything here is offline. Providers are stubs, and the securities directory
comes from the packaged seed lists the universe selector already falls back to.
"""

from __future__ import annotations

import datetime as dt

import pytest
from typer.testing import CliRunner

from claudetrade.cli import app
from claudetrade.config import AppConfig, reset_config_cache
from claudetrade.db.models import Security, SocialPostRow, SymbolSentimentDaily
from claudetrade.db.session import Database, reset_database_cache
from claudetrade.domain import SocialPost, SocialSource, SymbolAttention
from claudetrade.pipeline import Pipeline
from claudetrade.utils.timeutils import utc_now

runner = CliRunner()


class StubSocialProvider:
    """Returns a fixed post set and records the window it was asked for."""

    name = "stub_social"

    def __init__(self, posts: list[SocialPost]) -> None:
        self.posts = posts
        self.calls: list[dict[str, object]] = []

    def fetch_posts(self, *, since, until=None, symbols=None, limit=None):
        self.calls.append({"since": since, "until": until, "symbols": symbols})
        return list(self.posts)


class StubAttentionProvider:
    name = "stub_attention"

    def __init__(self) -> None:
        self.calls = 0

    def fetch_attention(self):
        self.calls += 1
        return [
            SymbolAttention(
                symbol="NVDA", community="all-stocks", mentions=42, upvotes=100, rank=1
            )
        ]


class ExplodingMarketProvider:
    """Any call at all is a test failure -- see this module's docstring."""

    name = "must_never_be_used"

    def __getattr__(self, item):
        raise AssertionError(
            f"an hourly social collection reached the market provider ({item})"
        )


def _post(text: str, *, minutes_ago: int = 30, external_id: str = "p1") -> SocialPost:
    return SocialPost(
        source=SocialSource.REDDIT,
        external_id=external_id,
        created_at=utc_now() - dt.timedelta(minutes=minutes_ago),
        text=text,
        score=25,
        author_hash=f"author-{external_id}",
        num_comments=4,
    )


@pytest.fixture
def config(tmp_app_config: AppConfig) -> AppConfig:
    # A tiny static universe keeps mention resolution deterministic and stops
    # the packaged seed lists (thousands of symbols) from dominating runtime.
    tmp_app_config.universe.source = "static"
    tmp_app_config.universe.static_symbols = ["NVDA", "AMD"]
    return tmp_app_config


@pytest.fixture
def pipeline(config: AppConfig, tmp_db: Database) -> Pipeline:
    built = Pipeline(config, tmp_db)
    built.market = ExplodingMarketProvider()
    built.earnings = ExplodingMarketProvider()
    built.social = []
    built.attention = []
    return built


class TestCollectSocial:
    def test_it_stores_posts_mentions_and_sentiment(self, pipeline, tmp_db) -> None:
        with tmp_db.session() as session:
            session.merge(Security(symbol="NVDA", name="NVIDIA Corp"))
        pipeline.social = [StubSocialProvider([_post("$NVDA looks strong here")])]

        result = pipeline.collect_social(lookback_hours=6)

        assert result.ingest.posts_inserted == 1
        assert result.ingest.mentions_inserted == 1
        assert result.sentiment_rows >= 1
        with tmp_db.read_session() as session:
            assert session.query(SocialPostRow).count() == 1
            rows = session.query(SymbolSentimentDaily).all()
        assert {r.symbol for r in rows} == {"NVDA"}
        assert "all" in {r.source for r in rows}

    def test_it_never_touches_the_market_or_earnings_providers(
        self, pipeline, tmp_db
    ) -> None:
        """The whole cost argument for an hourly cadence rests on this."""
        pipeline.social = [StubSocialProvider([_post("$NVDA up")])]
        pipeline.attention = [StubAttentionProvider()]

        result = pipeline.collect_social(lookback_hours=6)

        assert result.ingest.bars_inserted == 0
        assert result.ingest.earnings_upserted == 0
        assert result.ingest.securities_upserted == 0

    def test_the_lookback_window_is_what_the_providers_are_asked_for(
        self, pipeline
    ) -> None:
        provider = StubSocialProvider([])
        pipeline.social = [provider]

        pipeline.collect_social(lookback_hours=72)

        window = provider.calls[0]
        age_hours = (utc_now() - window["since"]).total_seconds() / 3600
        assert 71 < age_hours < 73
        # Open-ended: "up to now", never a fixed upper bound that would drop
        # posts written while the fetch was in flight.
        assert window["until"] is None

    def test_attention_is_collected_even_with_no_post_sources(
        self, pipeline, tmp_db
    ) -> None:
        """ApeWisdom keeps producing when Reddit is blocked and X has no
        cookie -- gating it on the post sources would throw that away."""
        with tmp_db.session() as session:
            session.merge(Security(symbol="NVDA", name="NVIDIA Corp"))
        attention = StubAttentionProvider()
        pipeline.attention = [attention]

        pipeline.collect_social(lookback_hours=6)

        assert attention.calls == 1
        with tmp_db.read_session() as session:
            rows = session.query(SymbolSentimentDaily).all()
        assert [(r.symbol, r.source, r.post_count) for r in rows] == [
            ("NVDA", "apewisdom:all-stocks", 42)
        ]

    def test_a_failing_provider_degrades_rather_than_aborting(
        self, pipeline, tmp_db
    ) -> None:
        from claudetrade.providers.base import ProviderError

        class DeadProvider:
            name = "dead"

            def fetch_posts(self, **_kwargs):
                raise ProviderError("rate limited")

        with tmp_db.session() as session:
            session.merge(Security(symbol="NVDA", name="NVIDIA Corp"))
        pipeline.social = [DeadProvider(), StubSocialProvider([_post("$NVDA moving")])]

        result = pipeline.collect_social(lookback_hours=6)

        assert result.degraded_sources == {"dead": "rate limited"}
        assert result.ingest.posts_inserted == 1

    def test_no_configured_source_says_so_instead_of_pretending(self, pipeline) -> None:
        result = pipeline.collect_social(lookback_hours=6)

        assert result.sentiment_rows == 0
        assert any("nothing to collect" in w for w in result.warnings)

    def test_repeated_collections_are_idempotent_on_the_same_posts(
        self, pipeline, tmp_db
    ) -> None:
        """Overlapping lookbacks are the point (a missed tick is recovered by
        the next one), so re-fetching the same post must not duplicate it."""
        with tmp_db.session() as session:
            session.merge(Security(symbol="NVDA", name="NVIDIA Corp"))
        pipeline.social = [StubSocialProvider([_post("$NVDA breaking out")])]

        pipeline.collect_social(lookback_hours=6)
        pipeline.collect_social(lookback_hours=6)

        with tmp_db.read_session() as session:
            assert session.query(SocialPostRow).count() == 1


class TestCollectCommand:
    """``claudetrade sentiment collect`` -- the manual twin of one tick."""

    @pytest.fixture
    def cli_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
        reset_config_cache()
        reset_database_cache()
        yield
        reset_config_cache()
        reset_database_cache()

    def test_it_collects_once_and_reports_readiness(self, cli_env, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def fake_collect(self, *, lookback_hours, progress_callback=None):
            from claudetrade.pipeline import PipelineResult

            captured["lookback_hours"] = lookback_hours
            captured["progress_callback"] = progress_callback
            return PipelineResult(sentiment_rows=7)

        monkeypatch.setattr(Pipeline, "collect_social", fake_collect)

        result = runner.invoke(app, ["sentiment", "collect", "--lookback-hours", "12"])

        assert result.exit_code == 0, result.output
        assert captured["lookback_hours"] == 12
        # The refresh-lock heartbeat rides the ordinary progress plumbing.
        assert captured["progress_callback"] is not None
        assert '"status": "collected"' in result.output
        assert '"sentiment_rows": 7' in result.output
        assert '"tier": "warming_up"' in result.output

    def test_it_is_recorded_as_operator_triggered_not_scheduled(
        self, cli_env, monkeypatch
    ) -> None:
        def fake_collect(self, *, lookback_hours, progress_callback=None):
            from claudetrade.pipeline import PipelineResult

            return PipelineResult()

        monkeypatch.setattr(Pipeline, "collect_social", fake_collect)

        result = runner.invoke(app, ["sentiment", "collect"])

        assert result.exit_code == 0, result.output
        assert '"entry_point": "cli"' in result.output

    def test_it_refuses_rather_than_racing_a_running_refresh(
        self, cli_env, monkeypatch
    ) -> None:
        from claudetrade.db import refresh_state_store
        from claudetrade.db.session import get_database

        called = {"n": 0}

        def fake_collect(self, *, lookback_hours, progress_callback=None):
            called["n"] += 1
            raise AssertionError("must not collect while the lock is held")

        monkeypatch.setattr(Pipeline, "collect_social", fake_collect)

        runner.invoke(app, ["init"])
        from claudetrade.config import get_config

        holder = refresh_state_store.try_acquire(get_database(get_config()), "webapi")
        assert holder.acquired

        result = runner.invoke(app, ["sentiment", "collect"])

        # Exit 0: a lock-contention skip is the single-flight lock working as
        # intended, not a failure -- Task Scheduler and other unattended
        # callers must not record it as a run failure. The JSON payload still
        # says "skipped" for anyone who wants to tell it apart from "collected".
        assert result.exit_code == 0
        assert '"status": "skipped"' in result.output
        assert "webapi" in result.output
        assert called["n"] == 0

    def test_a_genuine_failure_still_exits_nonzero(self, cli_env, monkeypatch) -> None:
        """Unlike a benign skip, an actual collection failure must still exit 1 --
        this is the case an unattended scheduler DOES need to see as a failure."""

        def fake_collect(self, *, lookback_hours, progress_callback=None):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(Pipeline, "collect_social", fake_collect)

        result = runner.invoke(app, ["sentiment", "collect"])

        assert result.exit_code == 1
        assert '"status": "failed"' in result.output
        assert "provider exploded" in result.output
