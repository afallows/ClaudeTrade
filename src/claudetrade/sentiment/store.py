"""The non-destructive stored-history read path, and collection coverage.

Two storage concerns the sentiment subsystem needs, neither of which may be
owned privately by the refresh path or by the maintenance path.

**Reading persisted posts.** ``Pipeline.refresh`` used to aggregate only the
posts one run had just fetched -- the providers reach back ~72 hours -- so
every daily aggregate was built from a single fetch and no rolling baseline
could ever accumulate, however long the installation ran. That breaks the
whole premise of the application (spotting *rising* mentions before price
moves), and it is what turned 1,830 posts into 14 aggregate rows on the
owner's production run. The read that fixes it already existed inside
:func:`sentiment.rebuild.rebuild_sentiment`, but calling *that* per refresh
would have been a data-loss bug: it deletes every ticker mention and every
aggregate row inside a 90-day window before rebuilding them. So only the
READ moved here. The deletes stayed in ``sentiment.rebuild``, reachable
only from ``claudetrade db rebuild-sentiment`` and the extraction-version
self-heal, where an operator (or an explicit upgrade) has asked for them.

Stored mentions (``ticker_mentions``) are deliberately NOT used to narrow
that read, tempting an index as they are: ``rebuild_sentiment`` clears that
table wholesale, so straight after any rebuild it is empty while the posts
it was derived from remain. A loader that pre-filtered posts by stored
mentions would then return nothing, and the rolling baseline would vanish
silently with no error anywhere. Mentions are re-derived from post text by
the CURRENT resolver instead -- which is also the property that keeps the
refresh path and the rebuild path producing identical aggregates from
identical stored input.

**Collection coverage.** A (symbol, session) with no stored aggregate row
reads as zero mentions. That is right when collection ran and nobody
posted, and wrong when the collector was down: an outage silently depresses
every baseline and then manufactures a surge the moment it ends. Nothing in
``symbol_sentiment_daily`` can tell those apart, because both are the
absence of a row. ``social_coverage`` records, per session and per source,
that collection actually ran and what it found -- so ``sentiment.history``
can divide a baseline by the sessions that were COLLECTED rather than by
the sessions that merely elapsed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from claudetrade.db.models import (
    Security,
    SocialCoverageRow,
    SocialPostRow,
)
from claudetrade.domain import SecurityInfo, SocialPost, SocialSource
from claudetrade.logging_setup import get_logger
from claudetrade.utils.timeutils import (
    ensure_utc,
    session_close_utc,
    session_for_instant,
    trading_day_range,
    utc_now,
)

if TYPE_CHECKING:  # annotation-only; avoids a package-level import cycle
    from claudetrade.db.session import Database

log = get_logger(__name__)

#: ``status`` values on ``social_coverage``. A session is "covered" only by
#: an ``ok`` row: ``failed`` is a positive record that we tried and could
#: not, which is exactly the state a baseline must not count as a real zero.
STATUS_OK = "ok"
STATUS_FAILED = "failed"


def read_utc(value: dt.datetime) -> dt.datetime:
    """Re-attach UTC to a datetime read back from the database.

    SQLite hands ``DateTime(timezone=True)`` columns back naive; every write
    went through ``ensure_utc``, so re-attaching UTC reproduces the stored
    instant (the same convention ``signals.ledger``'s read path uses).
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return ensure_utc(value)


# --------------------------------------------------------------------------
# Stored posts
# --------------------------------------------------------------------------


def load_stored_posts(
    db: Database,
    *,
    since: dt.datetime,
    until: dt.datetime | None = None,
) -> list[SocialPost]:
    """Reconstruct ``SocialPost`` domain objects from stored rows.

    Read-only and non-destructive -- this is the single implementation both
    ``Pipeline.refresh`` (rolling baseline) and
    ``sentiment.rebuild.rebuild_sentiment`` (explicit maintenance) use, so
    the two cannot drift into disagreeing about what "the stored history"
    means. The destructive part of a rebuild is not here; see the module
    docstring.

    Args:
        since: Inclusive lower bound on ``created_at``. Required, not
            optional: an unbounded read of ``social_posts`` is a full scan
            of the largest table in the database, and the callers all know
            how far back they actually need.
        until: Optional inclusive upper bound. Aggregation applies its own
            no-look-ahead filter per session (``aggregation.aggregate``
            keeps only ``created_at <= session_close``), so callers building
            aggregates do not need one -- it exists for callers reproducing
            a historical read exactly.
    """
    since = ensure_utc(since)
    until = ensure_utc(until) if until is not None else None

    # Coarse bound in SQL (padded by a day) so this is an indexed range scan
    # rather than a full table read; the authoritative comparison is redone
    # in Python below, after tz normalisation, because how an aware bound
    # compares against SQLite's naive storage is backend-dependent and this
    # path has no need to depend on it.
    stmt = select(SocialPostRow).where(
        SocialPostRow.created_at >= since - dt.timedelta(days=1)
    )
    if until is not None:
        stmt = stmt.where(SocialPostRow.created_at <= until + dt.timedelta(days=1))

    with db.read_session() as session:
        rows = session.execute(stmt).scalars().all()

    posts: list[SocialPost] = []
    for row in rows:
        created_at = read_utc(row.created_at)
        if created_at < since:
            continue
        if until is not None and created_at > until:
            continue
        posts.append(
            SocialPost(
                source=SocialSource(row.source),
                external_id=row.external_id,
                created_at=created_at,
                text=row.text,
                community=row.community,
                score=row.score,
                num_comments=row.num_comments,
                num_reposts=row.num_reposts,
                num_replies=row.num_replies,
                author_hash=row.author_hash,
                author_age_days=row.author_age_days,
                author_karma=row.author_karma,
                author_followers=row.author_followers,
                is_comment=row.is_comment,
                parent_id=row.parent_id,
                is_removed=row.is_removed,
                is_crosspost=row.is_crosspost,
                crosspost_parent=row.crosspost_parent,
                text_hash=row.text_hash,
                duplicate_group=row.duplicate_group,
                injection_risk=row.injection_risk,
                flair=row.flair,
            )
        )
    return posts


def load_securities_directory(db: Database) -> dict[str, SecurityInfo]:
    """Every stored security as the resolver's ``symbol -> SecurityInfo`` map."""
    with db.read_session() as session:
        rows = session.execute(select(Security)).scalars().all()
    return {
        r.symbol: SecurityInfo(
            symbol=r.symbol,
            name=r.name,
            exchange=r.exchange,
            sector=r.sector,
            industry=r.industry,
            market_cap_usd=r.market_cap_usd,
            shares_outstanding=r.shares_outstanding,
            is_etf=r.is_etf,
            is_leveraged_or_inverse=r.is_leveraged_or_inverse,
            listed_date=r.listed_date,
            delisted_date=r.delisted_date,
        )
        for r in rows
    }


# --------------------------------------------------------------------------
# Collection coverage
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SourceCollection:
    """One source's outcome for one collection run.

    ``source`` is the ``SocialSource`` value the provider emits (``reddit``,
    ``news``, ...) rather than the provider class's own name, so a source
    reads the same here as it does in ``symbol_sentiment_daily.source`` and
    in the history module -- an operator comparing the two is comparing like
    with like.
    """

    source: str
    ok: bool
    error: str = ""


def sessions_covered_by_fetch(
    since: dt.datetime, until: dt.datetime
) -> list[dt.date]:
    """Trading sessions a fetch spanning ``(since, until]`` actually closed out.

    A session ``S`` is covered when the fetch reached past its close without
    the close itself falling outside the fetch:
    ``since < session_close_utc(S) <= until``.

    Both halves matter. Requiring the close to be at or before ``until``
    keeps a still-OPEN session from being claimed as collected -- its
    information is by definition incomplete until it closes, and counting it
    as a fully collected zero would depress every baseline that includes it.
    Requiring the close to be after ``since`` is what makes the usual
    ~72-hour provider lookback recover the sessions an outage skipped: a
    refresh that runs after two days down still reaches back past both
    missed closes and correctly claims all three sessions.
    """
    since = ensure_utc(since)
    until = ensure_utc(until)
    if until <= since:
        return []
    candidates = trading_day_range(
        (since - dt.timedelta(days=1)).date(), (until + dt.timedelta(days=1)).date()
    )
    return [d for d in candidates if since < session_close_utc(d) <= until]


def record_collection_coverage(
    db: Database,
    *,
    sessions: Iterable[dt.date],
    outcomes: Sequence[SourceCollection],
    posts: Sequence[SocialPost] = (),
) -> int:
    """Record, per (session, source), that collection ran -- and how it went.

    Idempotent by construction: the grain is ``(session, source)`` and every
    write is an upsert, so re-running a refresh over the same window rewrites
    the same rows rather than accumulating evidence of collections that never
    separately happened.

    Two upsert rules that are not obvious:

    * **An ``ok`` row is never downgraded to ``failed``.** If Reddit answered
      on Monday evening and was rate-limited on Tuesday's re-run over the
      same window, Monday's session genuinely WAS collected; rewriting it as
      an outage would erase real coverage and make an honest zero look like
      missing data -- the exact confusion this table exists to end.
    * **``posts_collected`` takes the maximum, never the sum.** It is a
      diagnostic ("did anything actually arrive?"), and summing would make a
      re-run inflate it without a single new post being fetched.

    Args:
        sessions: Sessions this collection run covered -- normally
            :func:`sessions_covered_by_fetch` over the window the ingestor
            fetched.
        outcomes: One entry per configured source, whether or not it
            produced anything. A source that ran and found nothing is the
            whole point: that is a CONFIRMED zero, not missing data.
        posts: This run's posts, used only to count what landed in each
            (session, source). Attribution is by ``created_at`` through
            ``session_for_instant``, the same mapping the aggregates use.

    Returns:
        Number of rows inserted or updated.
    """
    sessions = sorted(set(sessions))
    if not sessions or not outcomes:
        return 0

    wanted = set(sessions)
    counts: dict[tuple[dt.date, str], int] = {}
    for post in posts:
        session_date = session_for_instant(ensure_utc(post.created_at))
        if session_date not in wanted:
            continue
        key = (session_date, post.source.value)
        counts[key] = counts.get(key, 0) + 1

    now = utc_now()
    written = 0
    # One short transaction: a handful of sessions times a handful of
    # sources is a dozen upserts at most, and no aggregation or network work
    # happens inside it (see build_sentiment's transaction-scope note).
    with db.session() as session:
        for session_date in sessions:
            for outcome in outcomes:
                row = (
                    session.query(SocialCoverageRow)
                    .filter_by(session=session_date, source=outcome.source)
                    .one_or_none()
                )
                if row is None:
                    row = SocialCoverageRow(
                        session=session_date,
                        source=outcome.source,
                        first_collected_at=now,
                    )
                    session.add(row)
                observed = counts.get((session_date, outcome.source), 0)
                if outcome.ok or row.status != STATUS_OK:
                    row.status = STATUS_OK if outcome.ok else STATUS_FAILED
                row.error = outcome.error
                row.posts_collected = max(row.posts_collected or 0, observed)
                row.updated_at = now
                written += 1
    return written


@dataclass(slots=True)
class CoverageWindow:
    """What collection coverage is known about a range of sessions.

    ``tracked_from`` is the load-bearing field. Coverage recording started
    when this feature shipped, so an installation upgraded yesterday has
    months of perfectly good aggregates and no coverage rows behind them.
    Reading that as "nothing was ever collected" would zero out every
    baseline overnight -- a far worse error than the one coverage exists to
    fix. So coverage only claims authority from the first session it ever
    recorded onwards; anything earlier is *unknown*, and unknown is treated
    as collected, which is exactly the assumption the code made before this
    table existed.
    """

    start: dt.date
    end: dt.date
    tracked_from: dt.date | None = None
    collected: dict[dt.date, set[str]] | None = None
    attempted: set[dt.date] | None = None

    def __post_init__(self) -> None:
        if self.collected is None:
            self.collected = {}
        if self.attempted is None:
            self.attempted = set()

    @property
    def tracked(self) -> bool:
        """Whether coverage says anything at all about this window."""
        return self.tracked_from is not None

    def is_collected(self, session: dt.date) -> bool:
        """Whether ``session`` may be trusted as a real, collected observation."""
        if self.tracked_from is None or session < self.tracked_from:
            return True  # pre-tracking: unknown, assumed collected -- see class docstring
        return session in (self.collected or {})

    def sources(self) -> set[str]:
        """Every source that successfully contributed inside the window."""
        out: set[str] = set()
        for names in (self.collected or {}).values():
            out |= names
        return out


def coverage_window(db: Database, *, start: dt.date, end: dt.date) -> CoverageWindow:
    """Load collection coverage for ``[start, end]`` in one read."""
    with db.read_session() as session:
        rows = session.execute(
            select(
                SocialCoverageRow.session,
                SocialCoverageRow.source,
                SocialCoverageRow.status,
            ).where(
                SocialCoverageRow.session >= start,
                SocialCoverageRow.session <= end,
            )
        ).all()
        # The horizon is deliberately global, not window-scoped: a window
        # entirely before tracking began must read as "unknown", and a
        # window entirely after it must read as "tracked but empty" -- both
        # of which need to know when tracking actually started.
        tracked_from = session.execute(
            select(func.min(SocialCoverageRow.session))
        ).scalar()

    collected: dict[dt.date, set[str]] = {}
    attempted: set[dt.date] = set()
    for session_date, source, status in rows:
        attempted.add(session_date)
        if status == STATUS_OK:
            collected.setdefault(session_date, set()).add(source)
    return CoverageWindow(
        start=start,
        end=end,
        tracked_from=tracked_from,
        collected=collected,
        attempted=attempted,
    )


__all__ = [
    "STATUS_FAILED",
    "STATUS_OK",
    "CoverageWindow",
    "SourceCollection",
    "coverage_window",
    "load_securities_directory",
    "load_stored_posts",
    "read_utc",
    "record_collection_coverage",
    "sessions_covered_by_fetch",
]
