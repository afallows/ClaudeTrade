"""``claudetrade db rebuild-sentiment`` -- recomputing stored aggregates from
stored posts with the current extraction/classifier code, so fixed bugs stop
echoing out of ``symbol_sentiment_daily`` forever (QA F14: the trending list
kept serving junk symbols written by a since-fixed extractor).
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from claudetrade.cli import app
from claudetrade.config import reset_config_cache
from claudetrade.db.models import Security, SocialPostRow, SymbolSentimentDaily
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


def _seed() -> None:
    from claudetrade.config import get_config

    config = get_config(reload=True)
    db = get_database(config)
    with db.session() as session:
        session.merge(Security(symbol="AMZN", name="Amazon.com Inc"))
        texts = [
            "$AMZN crushed earnings, very bullish. Buying more calls!",
            "$AMZN beat expectations, guidance raised. Loading up here.",
            "Great earnings from $AMZN, this is going higher.",
            "$AMZN to the moon after that blowout quarter.",
            "$AMZN printing money, growth is back. Long term hold.",
            "Undervalued even after the pop, adding $AMZN.",
            "$AMZN best stock in my portfolio right now.",
            "I was bearish but $AMZN proved me wrong, crushed it.",
            "Solid quarter from $AMZN, staying long.",
            "$AMZN breaking out on strong volume today.",
        ]
        for i, text in enumerate(texts):
            session.add(
                SocialPostRow(
                    source="reddit",
                    external_id=f"t3_rebuild{i}",
                    # Naive on purpose: SQLite returns DateTime(timezone=True)
                    # columns naive, and the command must survive exactly that.
                    created_at=dt.datetime.now(tz=dt.UTC).replace(tzinfo=None)
                    - dt.timedelta(hours=30 + i),
                    text=text,
                    author_hash=f"author{i}",
                    score=25,
                )
            )
        # A stale junk aggregate the old extractor left behind -- the exact
        # rows the QA session saw topping the trending list.
        session.add(
            SymbolSentimentDaily(
                symbol="YOU",
                session=dt.date.today() - dt.timedelta(days=2),
                source="all",
                post_count=1851,
            )
        )


def test_rebuild_replaces_junk_aggregates_with_recomputed_rows(cli_env):
    _seed()
    result = runner.invoke(app, ["db", "rebuild-sentiment"])
    assert result.exit_code == 0, result.output

    from claudetrade.config import get_config

    db = get_database(get_config(reload=True))
    with db.read_session() as session:
        rows = session.execute(select(SymbolSentimentDaily)).scalars().all()
    symbols = {r.symbol for r in rows}
    assert "YOU" not in symbols  # junk aggregate cleared
    assert "AMZN" in symbols  # rebuilt from stored posts with current code
    amzn = [r for r in rows if r.symbol == "AMZN"]
    # The rebuilt rows must carry the repaired classifier's output: real
    # polarity and usable confidence, not the degenerate zeros.
    assert any(r.raw_sentiment > 0.1 for r in amzn)
    assert any(r.confidence > 0.2 for r in amzn)


def test_cli_is_a_thin_wrapper_over_the_importable_core(cli_env):
    """The command body now lives in ``sentiment.rebuild.rebuild_sentiment``
    (the same core the bootstrap self-heal calls); the CLI must surface that
    core's summary verbatim -- including the original JSON keys, which are
    operator-visible output shape."""
    import json
    import re

    _seed()
    result = runner.invoke(app, ["db", "rebuild-sentiment", "--days", "60"])
    assert result.exit_code == 0, result.output

    # The runner captures log lines alongside stdout; fish the JSON block out.
    match = re.search(r'\{\s*"posts_considered".*?\}', result.output, re.S)
    assert match is not None, result.output
    payload = json.loads(match.group(0))
    assert set(payload) == {
        "posts_considered",
        "mentions_deleted",
        "sentiment_aggregates_deleted",
        "sentiment_rows_rebuilt",
        "symbols_affected",
    }
    assert payload["posts_considered"] == 10
    assert payload["sentiment_rows_rebuilt"] >= 1
    assert "Rebuilt" in result.output


def test_cli_rebuild_records_the_extraction_version(cli_env):
    """An explicit manual rebuild brings the version stamp current too, so
    the bootstrap self-heal will not immediately redo the same work."""
    from claudetrade.config import get_config
    from claudetrade.sentiment import EXTRACTION_VERSION
    from claudetrade.sentiment.rebuild import record_extraction_version, stored_extraction_version

    _seed()
    db = get_database(get_config(reload=True))
    record_extraction_version(db, EXTRACTION_VERSION - 1)  # simulate stale stamp

    result = runner.invoke(app, ["db", "rebuild-sentiment"])
    assert result.exit_code == 0, result.output
    assert stored_extraction_version(db) == EXTRACTION_VERSION


def test_rebuild_without_securities_aborts_before_deleting(cli_env):
    from claudetrade.config import get_config

    db = get_database(get_config(reload=True))
    with db.session() as session:
        session.execute(
            SymbolSentimentDaily.__table__.delete()  # start clean
        )
        session.execute(Security.__table__.delete())
        session.add(
            SymbolSentimentDaily(
                symbol="KEEP",
                session=dt.date.today(),
                source="all",
                post_count=5,
            )
        )

    result = runner.invoke(app, ["db", "rebuild-sentiment"])
    assert result.exit_code == 1

    with db.read_session() as session:
        remaining = session.execute(select(SymbolSentimentDaily)).scalars().all()
    # The abort must not have wiped what it could not rebuild.
    assert {r.symbol for r in remaining} == {"KEEP"}
