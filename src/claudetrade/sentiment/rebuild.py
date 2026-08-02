"""Rebuild stored sentiment aggregates from stored posts, and self-heal them.

Stored ``symbol_sentiment_daily`` rows outlive the extraction code that wrote
them. QA F14 and F25 both hit the same failure shape: the trending list kept
surfacing common-word "tickers" (AS, YOU, AN, DAY, ... -- all genuine NYSE/
Nasdaq listings, resolved out of ordinary English by a since-fixed extractor)
with the all-neutral ``bull_bear_ratio == 1.0`` signature of the pre-repair
classifier, *weeks after both bugs were fixed*, because ``get_trending`` reads
the stored aggregates and nothing ever revisited them. ``claudetrade db
rebuild-sentiment`` existed for exactly this, but a fix that requires the
operator to read a runbook is a fix that does not happen.

This module therefore provides two layers:

* :func:`rebuild_sentiment` -- the importable core of the CLI command
  (``cli.db_rebuild_sentiment`` is now a thin wrapper around it): clear the
  stored mentions and the aggregate rows inside the window, re-aggregate from
  the sanitised posts already on disk using the CURRENT resolver/classifier,
  and return a summary. Pure aggregation over stored rows -- no network.

  **This module owns the destruction, and only this module.** The READ side
  -- reconstructing ``SocialPost`` objects from ``social_posts`` -- moved to
  ``sentiment.store`` so ``Pipeline.refresh`` can build a rolling baseline
  from persisted history without inheriting the deletes. Calling
  ``rebuild_sentiment`` per refresh instead would wipe every ticker mention
  and every aggregate in a 90-day window on every run; the shared loader
  exists precisely so nobody is tempted to.
* :func:`ensure_extraction_version` -- the bootstrap self-heal.
  ``sentiment.entity_resolution.EXTRACTION_VERSION`` names the extraction
  code generation; the version the stored aggregates were last built with is
  stamped into the existing ``settings_kv`` table (no new table, no schema
  migration). At every ``db.migrations.init_database`` -- which every entry
  point's ``Pipeline.bootstrap`` passes through -- a stale stamp plus stored
  posts triggers one automatic rebuild, after which the current version is
  recorded. The owner's next ``git pull`` + any command heals the database
  without anyone being told to run anything.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select

from claudetrade.db.models import (
    Security,
    SettingKV,
    SocialPostRow,
    SymbolSentimentDaily,
    TickerMentionRow,
)
from claudetrade.sentiment.entity_resolution import EXTRACTION_VERSION
from claudetrade.sentiment.store import load_securities_directory, load_stored_posts
from claudetrade.utils.timeutils import utc_now

if TYPE_CHECKING:  # imported lazily at call time -- see rebuild_sentiment()
    from claudetrade.config import AppConfig
    from claudetrade.db.session import Database

log = logging.getLogger(__name__)

#: How far back (calendar days) the default rebuild reaches. Matches the CLI
#: flag's default; aggregation decay makes anything older weightless anyway.
DEFAULT_REBUILD_DAYS = 90

#: ``settings_kv`` key under which the extraction version the stored
#: aggregates were last built with is recorded, as ``{"version": int}``.
#: Piggybacks the existing operator-settings KV table rather than adding a
#: dedicated meta table -- no schema change, so it composes cleanly with any
#: other concurrently-added tables.
EXTRACTION_VERSION_KEY = "sentiment_extraction_version"


class RebuildUnavailableError(RuntimeError):
    """The rebuild cannot run safely (raised BEFORE anything is deleted)."""


def stored_extraction_version(db: Database) -> int:
    """Extraction version stamped in the database; 0 when never stamped.

    0 is exactly what a pre-versioning database reports, which is what makes
    every legacy database read as "older than the current extractor" and
    self-heal on first bootstrap after upgrading.
    """
    with db.read_session() as session:
        row = session.get(SettingKV, EXTRACTION_VERSION_KEY)
    if row is None:
        return 0
    try:
        return int((row.value or {}).get("version", 0))
    except (TypeError, ValueError):
        return 0


def record_extraction_version(db: Database, version: int = EXTRACTION_VERSION) -> None:
    """Stamp ``version`` as the extraction generation the aggregates match."""
    with db.session() as session:
        row = session.get(SettingKV, EXTRACTION_VERSION_KEY)
        if row is None:
            row = SettingKV(key=EXTRACTION_VERSION_KEY)
            session.add(row)
        row.value = {"version": int(version)}
        row.updated_at = utc_now()


def rebuild_sentiment(
    config: AppConfig, db: Database, *, days: int = DEFAULT_REBUILD_DAYS
) -> dict[str, Any]:
    """Recompute daily sentiment aggregates from the posts already stored.

    Uses the CURRENT entity-resolution and classifier code: every stored
    ticker-mention row is cleared (they are re-derivable diagnostics), the
    aggregate rows inside the ``days`` window are cleared, and the window is
    re-aggregated from the sanitised posts on disk. History older than the
    window (whose posts may already be pruned) survives untouched. No
    network access -- this is bounded aggregation over stored rows, though on
    a large database it can take on the order of a minute.

    On success the current ``EXTRACTION_VERSION`` is recorded, so the
    bootstrap self-heal (:func:`ensure_extraction_version`) knows the stored
    aggregates now match the running code.

    Args:
        config: Application config (sentiment thresholds/lookbacks).
        db: Open database handle.
        days: Rebuild sessions this many calendar days back from today.

    Returns:
        Summary dict: ``posts_considered``, ``mentions_deleted``,
        ``sentiment_aggregates_deleted``, ``sentiment_rows_rebuilt``,
        ``symbols_affected``, ``window_start``, ``window_end``.

    Raises:
        RebuildUnavailableError: when no securities are stored -- raised
            before any delete, so aborting never leaves the aggregates wiped
            with nothing rebuilt in their place.
    """
    # Local import: pipeline.py imports the sentiment package at module load;
    # importing it back at module level here would be a cycle.
    from claudetrade.pipeline import Pipeline

    end = utc_now().date()
    start = end - dt.timedelta(days=days)
    # Posts strictly older than the window cannot contribute to any rebuilt
    # session (aggregation decay makes them weightless well before this), but
    # the fetch is padded by the aggregation lookback so the earliest rebuilt
    # sessions still see their own trailing context.
    post_cutoff = dt.datetime.combine(
        start - dt.timedelta(days=config.sentiment.lookback_days), dt.time.min, tzinfo=dt.UTC
    )

    # Reads first, and OUTSIDE any write transaction: the loader below is the
    # same non-destructive read ``Pipeline.refresh`` uses for its rolling
    # baseline (``sentiment.store``), so the two paths cannot drift apart
    # about what "the stored history" is. Everything destructive stays in
    # this function, after the refusal check.
    directory = load_securities_directory(db)
    if not directory:
        # Checked BEFORE any delete: aborting must not leave the stored
        # aggregates wiped with nothing rebuilt in their place.
        raise RebuildUnavailableError(
            "No securities stored -- run 'claudetrade refresh' first."
        )
    posts = load_stored_posts(db, since=post_cutoff)

    with db.session() as session:
        # Mentions are re-derivable diagnostics and are cleared wholesale;
        # aggregates are only cleared inside the rebuild window, so history
        # older than the window (whose posts may already be pruned) survives.
        # Widen ``days`` to reach further back.
        mentions_deleted = session.execute(delete(TickerMentionRow)).rowcount
        aggregates_deleted = session.execute(
            delete(SymbolSentimentDaily).where(SymbolSentimentDaily.session >= start)
        ).rowcount

    pipeline = Pipeline(config, db)
    rows_written = pipeline.build_sentiment(
        posts=posts, directory=directory, start=start, end=end
    )
    with db.read_session() as session:
        symbols_affected = (
            session.execute(
                select(func.count(func.distinct(SymbolSentimentDaily.symbol))).where(
                    SymbolSentimentDaily.session >= start
                )
            ).scalar()
            or 0
        )

    record_extraction_version(db)
    return {
        "posts_considered": len(posts),
        "mentions_deleted": int(mentions_deleted),
        "sentiment_aggregates_deleted": int(aggregates_deleted),
        "sentiment_rows_rebuilt": int(rows_written),
        "symbols_affected": int(symbols_affected),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
    }


def ensure_extraction_version(
    db: Database, config: AppConfig | None = None
) -> dict[str, Any] | None:
    """Bootstrap self-heal: rebuild stale aggregates, then stamp the version.

    Called from ``db.migrations.init_database`` (after migrations have run,
    so the tables exist), which every entry point passes through. Decision
    table, in order:

    * stored version already current -- nothing to do (the common case; two
      indexed point reads).
    * no social posts stored -- nothing to rebuild; just record the current
      version so this check stays a no-op forever after.
    * posts stored but no ``config`` at this seam (bare ``init_database(db)``
      callers, e.g. tests) -- defer, unstamped, so the next config-carrying
      bootstrap heals.
    * posts stored but no securities -- the rebuild would refuse anyway
      (nothing to resolve against); defer, unstamped, and say so.
    * otherwise run :func:`rebuild_sentiment` (which stamps on success).

    A rebuild failure is logged with its traceback and swallowed: stale
    aggregates are the status quo this heals, not a reason to take every
    command down at startup. The stamp is only written on success, so the
    next bootstrap retries.

    Returns:
        The rebuild summary when a rebuild ran, else ``None``.
    """
    stored = stored_extraction_version(db)
    if stored >= EXTRACTION_VERSION:
        return None

    with db.read_session() as session:
        has_posts = session.execute(select(SocialPostRow.id).limit(1)).first() is not None
        has_securities = session.execute(select(Security.symbol).limit(1)).first() is not None

    if not has_posts:
        record_extraction_version(db)
        log.debug(
            "no social posts stored; sentiment extraction version recorded as v%d "
            "with nothing to rebuild",
            EXTRACTION_VERSION,
        )
        return None
    if config is None:
        log.debug(
            "stored sentiment aggregates predate extraction v%d but no config is "
            "available at this seam; deferring the rebuild to the next bootstrap",
            EXTRACTION_VERSION,
        )
        return None
    if not has_securities:
        log.warning(
            "stored sentiment aggregates predate extraction v%d but no securities are "
            "stored to resolve against; run 'claudetrade refresh' -- the rebuild will "
            "run automatically at the next start-up after that",
            EXTRACTION_VERSION,
        )
        return None

    log.info(
        "stored sentiment aggregates were built by extraction v%d < v%d; rebuilding "
        "them from stored posts with the current resolver/classifier (offline "
        "aggregation -- can take about a minute on a large database)...",
        stored,
        EXTRACTION_VERSION,
    )
    try:
        summary = rebuild_sentiment(config, db)
    except Exception:
        log.exception(
            "automatic sentiment rebuild failed; stored aggregates are unchanged and "
            "the version stamp was not advanced, so the next start-up retries. Run "
            "'claudetrade db rebuild-sentiment' manually to see the error interactively."
        )
        return None
    log.info(
        "sentiment self-heal complete: rebuilt %d aggregate row(s) across %d symbol(s) "
        "from %d stored post(s), clearing %d stale aggregate(s) and %d stale mention(s); "
        "extraction version recorded as v%d",
        summary["sentiment_rows_rebuilt"],
        summary["symbols_affected"],
        summary["posts_considered"],
        summary["sentiment_aggregates_deleted"],
        summary["mentions_deleted"],
        EXTRACTION_VERSION,
    )
    return summary


__all__ = [
    "DEFAULT_REBUILD_DAYS",
    "EXTRACTION_VERSION_KEY",
    "RebuildUnavailableError",
    "ensure_extraction_version",
    "rebuild_sentiment",
    "record_extraction_version",
    "stored_extraction_version",
]
