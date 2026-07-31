"""``claudetrade db purge-synthetic`` -- cleaning up a database that got
fabricated posts from the (former) synthetic-by-default social providers.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from claudetrade.cli import app
from claudetrade.config import reset_config_cache
from claudetrade.db.models import SocialPostRow, SymbolSentimentDaily, TickerMentionRow
from claudetrade.db.session import get_database, reset_database_cache

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
    reset_config_cache()
    reset_database_cache()
    runner.invoke(app, ["init"])
    yield tmp_path
    reset_config_cache()
    reset_database_cache()


def _seed(tmp_path) -> None:
    from claudetrade.config import get_config

    config = get_config(reload=True)
    db = get_database(config)
    with db.session() as session:
        synthetic_post = SocialPostRow(
            source="reddit",
            external_id="synthetic-11-3",
            created_at=dt.datetime.now(tz=dt.UTC),
            text="fabricated post",
        )
        real_post = SocialPostRow(
            source="reddit",
            external_id="t3_real123",
            created_at=dt.datetime.now(tz=dt.UTC),
            text="a genuine reddit post",
        )
        session.add_all([synthetic_post, real_post])
        session.flush()
        session.add(
            TickerMentionRow(
                post_id=synthetic_post.id, symbol="AAPL", confidence=0.9, method="cashtag"
            )
        )
        session.add(
            TickerMentionRow(post_id=real_post.id, symbol="MSFT", confidence=0.9, method="cashtag")
        )
        session.add(
            SymbolSentimentDaily(symbol="AAPL", session=dt.date(2024, 1, 2), source="all")
        )


def test_purge_synthetic_removes_only_synthetic_posts_and_their_mentions(cli_env):
    _seed(cli_env)

    result = runner.invoke(app, ["db", "purge-synthetic"])
    assert result.exit_code == 0, result.output

    from claudetrade.config import get_config

    db = get_database(get_config(reload=True))
    with db.read_session() as session:
        remaining_posts = session.execute(select(SocialPostRow)).scalars().all()
        remaining_mentions = session.execute(select(TickerMentionRow)).scalars().all()
        remaining_aggregates = session.execute(select(SymbolSentimentDaily)).scalars().all()

    assert [p.external_id for p in remaining_posts] == ["t3_real123"]
    assert [m.symbol for m in remaining_mentions] == ["MSFT"]
    # Aggregates cannot be attributed to one source without a recompute --
    # ALL of them are cleared, documented in the command's own output.
    assert remaining_aggregates == []


def test_purge_synthetic_reports_counts(cli_env):
    _seed(cli_env)
    result = runner.invoke(app, ["db", "purge-synthetic"])
    assert result.exit_code == 0, result.output
    assert '"posts_deleted": 1' in result.output
    assert '"mentions_deleted": 1' in result.output
    assert '"sentiment_aggregates_deleted": 1' in result.output


def test_purge_synthetic_is_a_no_op_on_a_clean_database(cli_env):
    result = runner.invoke(app, ["db", "purge-synthetic"])
    assert result.exit_code == 0, result.output
    assert "nothing to purge" in result.output.lower()
