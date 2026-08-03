"""Database schema.

Written against the SQLAlchemy 2.0 ORM using only portable column types, so the
same models run on SQLite (default) and PostgreSQL (documented migration path,
ADR-0003). Notes on the design:

* **Append-only where it matters.** ``signals``, ``signal_revisions``,
  ``paper_trades`` and ``audit_log`` are never updated in place. A correction is
  a new revision row. This is enforced in ``claudetrade.signals.ledger`` and
  guarded by a database trigger created in the migration for SQLite.
* **Reproducibility columns.** Every generated artefact carries
  ``code_version``, ``config_hash``, ``strategy_version`` and
  ``data_snapshot_hash``.
* **UTC everywhere.** ``DateTime(timezone=True)`` columns hold UTC instants;
  ``Date`` columns hold exchange trading dates.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base with a JSON type map suitable for SQLite and Postgres."""

    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------


class SchemaVersion(Base):
    """One row per applied migration; the migration runner is idempotent."""

    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    applied_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    checksum: Mapped[str] = mapped_column(String(64), default="")


class SettingKV(Base):
    """Operator settings that belong in the database rather than the config file
    (window layout, watchlists, acknowledged warnings)."""

    __tablename__ = "settings_kv"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditLog(Base):
    """Append-only record of security- and integrity-relevant actions."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    actor: Mapped[str] = mapped_column(String(80), default="system")
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity: Mapped[str] = mapped_column(String(80), default="")
    entity_id: Mapped[str] = mapped_column(String(80), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    code_version: Mapped[str] = mapped_column(String(60), default="")


# --------------------------------------------------------------------------
# Reference and market data
# --------------------------------------------------------------------------


class Security(Base):
    """Listed (or formerly listed) security.

    ``delisted_date`` is retained rather than the row being deleted -- removing
    dead companies is exactly how survivorship bias gets into a backtest.
    """

    __tablename__ = "securities"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    exchange: Mapped[str] = mapped_column(String(20), default="", index=True)
    sector: Mapped[str] = mapped_column(String(80), default="", index=True)
    industry: Mapped[str] = mapped_column(String(120), default="")
    market_cap_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    shares_outstanding: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_etf: Mapped[bool] = mapped_column(Boolean, default=False)
    is_leveraged_or_inverse: Mapped[bool] = mapped_column(Boolean, default=False)
    listed_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    delisted_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True, index=True)
    avg_dollar_volume_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    short_interest_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    implied_volatility_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    aliases: Mapped[list[SymbolAlias]] = relationship(
        back_populates="security", cascade="all, delete-orphan"
    )


class SymbolAlias(Base):
    """Former tickers, company-name variants and common abbreviations.

    Feeds entity resolution: a post naming ``Facebook`` should resolve to
    ``META``, and a 2019 post naming ``FB`` should too.
    """

    __tablename__ = "symbol_aliases"
    __table_args__ = (
        UniqueConstraint("symbol", "alias", "kind", name="uq_alias"),
        Index("ix_alias_lookup", "alias_normalised"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("securities.symbol"), index=True)
    alias: Mapped[str] = mapped_column(String(200))
    alias_normalised: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30), default="name")  # name|former_symbol|nickname
    valid_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    security: Mapped[Security] = relationship(back_populates="aliases")


class PriceBar(Base):
    """Daily OHLCV. One row per (symbol, session, source)."""

    __tablename__ = "price_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "session", "source", name="uq_bar"),
        Index("ix_bar_symbol_session", "symbol", "session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(40), default="unknown")
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class IntradayBar(Base):
    """Intraday bars, kept separate so daily queries stay narrow."""

    __tablename__ = "intraday_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "ts", "interval_minutes", "source", name="uq_intraday"),
        Index("ix_intraday_symbol_ts", "symbol", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=5)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(40), default="unknown")


class SymbolFetchHealth(Base):
    """Per-symbol provider-fetch health, driving the refresh quarantine.

    A row exists only while a symbol is failing: ``data.ingest.DataIngestor``
    records a failure when a refresh's FULL provider chain (primary plus
    every fallback) produced zero bars for a requested symbol, and deletes
    the row again on the first success -- so this table is naturally small
    and ``claudetrade db fetch-health`` lists exactly the problem names.
    After three consecutive full-chain failures the symbol is quarantined
    (``quarantined_until``) and skipped by the expensive per-symbol fetch
    paths for a week, which is what stops a dead ticker from burning a
    dataForTicker probe + a yahoo chart call on every single refresh (the
    owner's "many symbol failures" retry burn). Stored history, universe
    membership and ``Security.delisted_date`` are all untouched -- this
    gates *fetching only*, and expiry retries the symbol automatically.
    """

    __tablename__ = "symbol_fetch_health"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str] = mapped_column(String(300), default="")
    quarantined_until: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CorporateActionRow(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint("symbol", "session", "kind", name="uq_corp_action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(30))
    ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(40), default="")


class EarningsEventRow(Base):
    """Earnings calendar entry.

    ``as_of`` records when this row's information became known, which is what
    the backtester filters on to avoid earnings-date leakage: a date that was
    only announced two weeks before the report must not be visible a year
    earlier.
    """

    __tablename__ = "earnings_events"
    __table_args__ = (
        UniqueConstraint("symbol", "report_date", "source", name="uq_earnings"),
        Index("ix_earnings_symbol_date", "symbol", "report_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    report_date: Mapped[dt.date] = mapped_column(Date, index=True)
    session: Mapped[str] = mapped_column(String(10), default="unknown")
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    eps_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    surprise_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="")
    as_of: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


# --------------------------------------------------------------------------
# Social data
# --------------------------------------------------------------------------


class SocialPostRow(Base):
    """A sanitised social post.

    Raw text is *not* stored verbatim by default; ``text`` holds the sanitised
    form and ``raw_ref`` an optional pointer (permalink id) so the original can
    be re-fetched from the source if licensing permits. Author usernames are
    never stored -- only ``author_hash``.
    """

    __tablename__ = "social_posts"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_social_post"),
        Index("ix_social_created", "source", "created_at"),
        Index("ix_social_texthash", "text_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(20), index=True)
    external_id: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    text: Mapped[str] = mapped_column(Text, default="")
    text_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    community: Mapped[str] = mapped_column(String(80), default="", index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    num_comments: Mapped[int] = mapped_column(Integer, default=0)
    num_reposts: Mapped[int] = mapped_column(Integer, default=0)
    num_replies: Mapped[int] = mapped_column(Integer, default=0)
    author_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    author_age_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    author_karma: Mapped[float | None] = mapped_column(Float, nullable=True)
    author_followers: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_comment: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_removed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_crosspost: Mapped[bool] = mapped_column(Boolean, default=False)
    crosspost_parent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    duplicate_group: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    injection_risk: Mapped[float] = mapped_column(Float, default=0.0)
    raw_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Reddit's native ``link_flair_text`` (e.g. "DD", "YOLO", "News"),
    #: nullable -- absent for posts with no flair and for every non-Reddit
    #: source. See ``domain.SocialPost.flair`` for the field's provenance
    #: and how it is used as a scoring prior. Added by migration 004.
    flair: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: The author's own directional tag on the post, normalised to
    #: ``"bullish"``/``"bearish"`` (Stocktwits' ``entities.sentiment.basic``;
    #: every other source leaves this NULL because it has no such concept).
    #: Nullable and *three*-valued on purpose -- NULL means "this author did
    #: not tag the post", which is a different fact from "this author called
    #: it neutral", and collapsing the two would invent opinions nobody
    #: expressed. See ``domain.SocialPost.sentiment_prior`` for why it is a
    #: prior hint rather than a label we vouch for. Added by migration 008.
    sentiment_prior: Mapped[str | None] = mapped_column(String(10), nullable=True)


class TickerMentionRow(Base):
    """Resolved symbol reference within a post, with resolution confidence."""

    __tablename__ = "ticker_mentions"
    __table_args__ = (
        UniqueConstraint("post_id", "symbol", name="uq_mention"),
        Index("ix_mention_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("social_posts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    method: Mapped[str] = mapped_column(String(30), default="")
    matched_text: Mapped[str] = mapped_column(String(120), default="")
    context: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SentimentRecordRow(Base):
    """Per-post, per-symbol classifier output."""

    __tablename__ = "sentiment_records"
    __table_args__ = (
        UniqueConstraint("post_id", "symbol", "classifier", name="uq_sentiment_record"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("social_posts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    classifier: Mapped[str] = mapped_column(String(40), default="rules")
    model: Mapped[str] = mapped_column(String(80), default="")
    prompt_version: Mapped[str] = mapped_column(String(20), default="")
    scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SocialCoverageRow(Base):
    """Proof that social collection actually ran for a (session, source).

    ``symbol_sentiment_daily`` is sparse on purpose -- a row exists only for
    a symbol/session that gained data -- which makes "no row" ambiguous in
    the one way that matters: it means *nobody posted* when the collector
    ran, and it means *we do not know* when the collector was down. Those
    are opposite facts. A collection outage read as a run of confirmed zeros
    silently depresses every baseline and then manufactures a surge the
    moment collection resumes, which is precisely the false positive this
    application exists not to produce.

    So collection records itself here, per session and source, whether or
    not it found anything: an ``ok`` row with ``posts_collected == 0`` is a
    CONFIRMED zero, a ``failed`` row is a known outage, and no row at all
    (at or after coverage tracking began) is a session nobody even
    attempted. ``sentiment.history`` divides baselines by collected sessions
    on the strength of this table; see ``sentiment.store`` for the write and
    read paths and for why sessions before the first recorded row are
    treated as unknown-but-collected rather than as gaps.
    """

    __tablename__ = "social_coverage"
    __table_args__ = (
        UniqueConstraint("session", "source", name="uq_social_coverage"),
        # Name must not collide with the auto-generated single-column index
        # SQLAlchemy derives from ``session``'s ``index=True``
        # (``ix_social_coverage_session``), or a fresh create_all and the
        # migration fight over the same index name.
        Index("ix_social_coverage_session_status", "session", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    #: ``SocialSource`` value ("reddit", "news", ...) or an attention
    #: provider's name ("apewisdom"), matching the labels used in
    #: ``symbol_sentiment_daily.source`` so the two can be read together.
    source: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok | failed
    posts_collected: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(300), default="")
    first_collected_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SymbolSentimentDaily(Base):
    """Aggregated daily sentiment per symbol and source.

    Aggregates are computed from posts whose ``created_at`` is at or before the
    session close, never after -- see ``sentiment.aggregation``.
    """

    __tablename__ = "symbol_sentiment_daily"
    __table_args__ = (
        UniqueConstraint("symbol", "session", "source", name="uq_sentiment_daily"),
        Index("ix_sentiment_daily_lookup", "symbol", "session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(20), default="all")
    post_count: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_authors: Mapped[int] = mapped_column(Integer, default=0)
    raw_sentiment: Mapped[float] = mapped_column(Float, default=0.0)
    engagement_weighted: Mapped[float] = mapped_column(Float, default=0.0)
    credibility_weighted: Mapped[float] = mapped_column(Float, default=0.0)
    unique_author_sentiment: Mapped[float] = mapped_column(Float, default=0.0)
    sentiment_acceleration: Mapped[float] = mapped_column(Float, default=0.0)
    mention_acceleration: Mapped[float] = mapped_column(Float, default=0.0)
    bull_bear_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    dispersion: Mapped[float] = mapped_column(Float, default=0.0)
    source_concentration: Mapped[float] = mapped_column(Float, default=0.0)
    duplicate_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    bot_risk: Mapped[float] = mapped_column(Float, default=0.0)
    manipulation_risk: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    total_engagement: Mapped[float] = mapped_column(Float, default=0.0)
    labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AdanosSnapshotRow(Base):
    """One Adanos platform's pre-aggregated buzz/sentiment reading for one
    symbol on one session.

    A dedicated table rather than an extension of ``symbol_sentiment_daily``
    (migration 010): that table's ``source``-keyed rows are built around
    ``data.ingest.DataIngestor.ingest_attention``'s "attention only, never
    polarity" contract -- callers (``mcp_server.get_trending``,
    ``sentiment.aggregation``) assume any row there either IS the strategy-
    scored ``"all"`` aggregate or carries no direction at all. Adanos breaks
    that assumption on purpose (it has real ``sentiment_score``/
    ``bullish_pct``/``bearish_pct``), plus per-platform ``trend``/
    ``trend_history`` that table has no columns for. A new table keeps that
    distinction structural instead of relying on every future reader to
    remember one ``source`` prefix is special.

    See ``domain.AdanosSnapshot`` for what each field means and
    ``providers.social.adanos`` for how rows are produced. Dedup/upsert is on
    ``(session, platform, symbol)`` -- a re-collection within the same
    session updates the existing row rather than duplicating it, matching
    ``ingest_attention``'s posture for ApeWisdom.
    """

    __tablename__ = "adanos_snapshots"
    __table_args__ = (
        UniqueConstraint("session", "platform", "symbol", name="uq_adanos_snapshot"),
        Index("ix_adanos_snapshot_lookup", "symbol", "session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    platform: Mapped[str] = mapped_column(String(20))
    company_name: Mapped[str] = mapped_column(String(120), default="")
    buzz_score: Mapped[float] = mapped_column(Float, default=0.0)
    #: ``mentions`` for x/reddit/news, ``trade_count`` for polymarket -- one
    #: column, source-specific meaning, matching how ``engagement`` already
    #: does multiple duty (upvotes vs liquidity vs source count) below.
    mentions: Mapped[int] = mapped_column(Integer, default=0)
    trend: Mapped[str] = mapped_column(String(10), default="")
    #: ``None`` when the vendor reported no score for this row -- distinct
    #: from ``0.0`` (measured neutral), the same absent-vs-neutral
    #: distinction ``SymbolSentimentDaily``'s attention rows preserve.
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    bullish_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    bearish_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: ``total_upvotes`` (x/reddit), ``total_liquidity`` (polymarket), or
    #: ``source_count`` -- distinct news outlets -- for the ``"news"``
    #: platform, which has no upvotes/likes/liquidity analogue.
    engagement: Mapped[float] = mapped_column(Float, default=0.0)
    #: The vendor's own 7-point trailing buzz series, oldest first, stored
    #: as reported -- see ``domain.AdanosSnapshot``.
    trend_history: Mapped[list[float]] = mapped_column(JSON, default=list)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AnalystSnapshotRow(Base):
    """One TipRanks-sourced analyst-sentiment snapshot for one symbol on one
    session.

    Parsed entirely from fields already present in the ``dataForTicker``
    ``overview`` payload ``providers.market.tipranks.TipRanksProvider``
    fetches (and caches) for reference data, market caps and earnings -- see
    ``providers.market.tipranks_analyst`` for the parser and
    ``domain.AnalystSnapshot`` for what each field means and where it comes
    from.

    A **mutable daily snapshot, not the immutable signal ledger** -- same
    posture as ``AdanosSnapshotRow`` immediately above (see that class's own
    docstring): dedup/upsert is on ``(session, symbol)``, so a re-refresh
    within the same session updates the existing row rather than duplicating
    it or triggering an append-only guard. No immutability trigger is
    installed for this table, matching migration 010's rationale for
    ``adanos_snapshots`` -- these rows are re-collected and upserted every
    cycle (see ``data.ingest.DataIngestor.ingest_analyst_snapshots``), not an
    audit trail.

    ``consensus_over_time``/``recent_rating_actions`` are stored as JSON
    lists of plain dicts (mirroring ``trend_history`` above and
    ``SymbolSentimentDaily.labels`` elsewhere in this module) rather than as
    child tables -- both are read-mostly, bounded-length, and always read
    whole alongside their parent row; a normalised child table would add
    join cost for a shape nothing here ever queries independently.
    """

    __tablename__ = "analyst_snapshots"
    __table_args__ = (
        UniqueConstraint("session", "symbol", name="uq_analyst_snapshot"),
        Index("ix_analyst_snapshot_lookup", "symbol", "session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    #: TipRanks' own opaque 1-5 rating scale (see ``domain.AnalystSnapshot``
    #: for the scale-direction caveat) -- ``None`` when the selected
    #: ``consensuses`` row was itself absent.
    consensus_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    buy_count: Mapped[int] = mapped_column(Integer, default=0)
    hold_count: Mapped[int] = mapped_column(Integer, default=0)
    sell_count: Mapped[int] = mapped_column(Integer, default=0)
    consensus_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_target_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_target_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_target_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_target_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    #: ``buy_count + hold_count + sell_count`` at write time -- stored
    #: rather than recomputed on read so a stored row is a complete,
    #: self-consistent record of what was seen (see
    #: ``domain.AnalystSnapshot``'s docstring for why this sum, not
    #: ``overview.numOfAnalysts``, is used).
    analyst_count: Mapped[int] = mapped_column(Integer, default=0)
    #: List of ``{date, buy, hold, sell, consensus, price_target}`` dicts,
    #: date-ascending, bounded by
    #: ``providers.market.tipranks_analyst.CONSENSUS_OVER_TIME_MAX``.
    consensus_over_time: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    #: List of ``{date, firm, analyst_name, rating_id, rating_label,
    #: action_id, action_label, price_target, old_price_target,
    #: analyst_stars, analyst_success_rate, included_in_consensus}`` dicts,
    #: date-descending (most recent first), bounded by
    #: ``providers.market.tipranks_analyst.RECENT_RATING_ACTIONS_MAX``.
    recent_rating_actions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    last_eps_surprise_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_earnings_estimate_eps: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class InstitutionalSnapshotRow(Base):
    """One TipRanks-sourced insider/hedge-fund ("institutional") sentiment
    snapshot for one symbol on one session.

    Parsed entirely from fields already present in the ``dataForTicker``
    ``overview`` payload -- see ``providers.market.tipranks_institutional``
    for the parser and scoring function, and ``domain.InstitutionalSnapshot``
    for what each field means and where it comes from.

    Same **mutable daily snapshot, not the immutable signal ledger** posture
    as ``AnalystSnapshotRow`` immediately above: dedup/upsert is on
    ``(session, symbol)``, no immutability trigger, re-collected and
    upserted every refresh cycle (see ``data.ingest.DataIngestor
    .ingest_institutional_snapshots``).

    The ``score``/``*_subscore``/``*_weight_applied``/``*_age_days`` columns
    are the computed output of ``tipranks_institutional.institutional_score``
    at INGEST time (``as_of`` = this row's own ``session``), stored alongside
    the raw fields that produced them -- a self-contained, diffable record:
    a caller reading an old row does not need to re-run the scoring formula
    (which may itself change constants over time) to see what this
    installation computed on the day it was collected. List-shaped sub-data
    is stored as JSON, mirroring ``AnalystSnapshotRow``'s own
    ``consensus_over_time``/``recent_rating_actions`` columns for the same
    reasons (read-mostly, bounded-length, always read whole).

    **Fed into ``signals.scoring.ComponentScores.institutional_sentiment`` as
    of ADR-0009** -- ``score`` below is read straight off this row by
    ``data.institutional.load_history``/``InstitutionalScorePoint`` at scan
    time, never recomputed. See ``domain.InstitutionalSnapshot``'s own
    docstring.
    """

    __tablename__ = "institutional_snapshots"
    __table_args__ = (
        UniqueConstraint("session", "symbol", name="uq_institutional_snapshot"),
        Index("ix_institutional_snapshot_lookup", "symbol", "session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    session: Mapped[dt.date] = mapped_column(Date, index=True)

    #: List of ``{month, year, shares_bought, insiders_buy_count,
    #: shares_sold, insiders_sell_count, trans_buy_count, trans_sell_count,
    #: trans_buy_amount, trans_sell_amount, informative_buy_count,
    #: informative_sell_count, informative_buy_amount,
    #: informative_sell_amount}`` dicts, month-ascending, bounded by
    #: ``tipranks_institutional.INSIDER_MONTHLY_MAX``.
    insider_monthly: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    #: This module's own derived trailing-3-month net insider $ flow (see
    #: ``tipranks_institutional._insider_net_3m_usd``) -- the figure the
    #: insider scoring axis actually uses.
    insider_net_3m_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: The vendor's own ``overview.insiderslast3MonthsSum`` figure, kept
    #: separately for display/cross-check -- see
    #: ``domain.InstitutionalSnapshot``'s docstring for why the two are not
    #: assumed to match exactly.
    insider_net_3m_usd_vendor: Mapped[float | None] = mapped_column(Float, nullable=True)
    insider_confidence_stock_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    insider_confidence_sector_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    insider_confidence_raw_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_of_insiders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: List of ``{name, is_officer, is_director, is_ten_percent_owner,
    #: officer_title, action, operation_description, amount,
    #: number_of_shares, r_date, estimated_shares_value, link}`` dicts,
    #: ranked by ``|estimated_shares_value|`` descending, bounded by
    #: ``tipranks_institutional.RECENT_INSIDER_TRANSACTIONS_MAX``.
    recent_insider_transactions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    hedge_fund_sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)
    hedge_fund_trend_action: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hedge_fund_trend_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: List of ``{date, holding_amount, institution_holding_percentage,
    #: net_shares_change, number_of_shares_bought, number_of_shares_sold,
    #: is_complete}`` dicts, date-ascending, bounded by
    #: ``tipranks_institutional.HEDGE_FUND_HOLDINGS_MAX``.
    hedge_fund_holdings_by_quarter: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    #: List of ``{manager_name, institution_name, action, effective_date,
    #: value, change_pct, change_amount, percentage_of_portfolio, stars,
    #: is_active}`` dicts, ranked by ``|change_amount|`` descending, bounded
    #: by ``tipranks_institutional.NOTABLE_HOLDER_MOVES_MAX``.
    notable_holder_moves: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    market_cap_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: ``tipranks_institutional.institutional_score(snapshot, session)``'s
    #: output, computed and stored at ingest time -- see this class's own
    #: docstring for why.
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    insider_subscore: Mapped[float | None] = mapped_column(Float, nullable=True)
    insider_weight_applied: Mapped[float] = mapped_column(Float, default=0.0)
    insider_age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hedge_fund_subscore: Mapped[float | None] = mapped_column(Float, nullable=True)
    hedge_fund_weight_applied: Mapped[float] = mapped_column(Float, default=0.0)
    hedge_fund_age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --------------------------------------------------------------------------
# Features, signals, ledger
# --------------------------------------------------------------------------


class FeatureRow(Base):
    """Point-in-time feature vector for a (symbol, session).

    ``feature_version`` lets old rows survive a formula change without silently
    mixing definitions inside one backtest.
    """

    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint("symbol", "session", "feature_version", name="uq_feature"),
        Index("ix_feature_lookup", "symbol", "session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    feature_version: Mapped[str] = mapped_column(String(20), default="v1")
    values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RegimeRow(Base):
    __tablename__ = "market_regimes"
    __table_args__ = (UniqueConstraint("session", "model_version", name="uq_regime"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    regime: Mapped[str] = mapped_column(String(30))
    model_version: Mapped[str] = mapped_column(String(20), default="v1")
    trend_score: Mapped[float] = mapped_column(Float, default=0.0)
    breadth: Mapped[float] = mapped_column(Float, default=0.5)
    volatility_percentile: Mapped[float] = mapped_column(Float, default=0.5)
    realised_vol_annual: Mapped[float] = mapped_column(Float, default=0.0)
    risk_appetite: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SignalRow(Base):
    """Immutable generated signal.

    Never updated after insert. Status changes and corrections are appended to
    ``signal_revisions``; the current state of a signal is its latest revision.
    A SQLite trigger (installed by migration 002) rejects UPDATE and DELETE.
    """

    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signal_symbol_session", "symbol", "session"),
        Index("ix_signal_strategy", "strategy", "session"),
    )

    signal_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    company_name: Mapped[str] = mapped_column(String(200), default="")
    strategy: Mapped[str] = mapped_column(String(60), index=True)
    strategy_version: Mapped[str] = mapped_column(String(20), default="")
    direction: Mapped[str] = mapped_column(String(10))
    initial_status: Mapped[str] = mapped_column(String(20))
    reference_price: Mapped[float] = mapped_column(Float)
    price_as_of: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    overall_score: Mapped[float] = mapped_column(Float, index=True)
    confidence: Mapped[float] = mapped_column(Float)
    components: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    regime: Mapped[str] = mapped_column(String(30), default="unknown")
    next_earnings_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    days_to_earnings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    earnings_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    thesis: Mapped[str] = mapped_column(Text, default="")
    invalidation: Mapped[list[Any]] = mapped_column(JSON, default=list)
    exit_conditions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    risks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    data_freshness_hours: Mapped[float] = mapped_column(Float, default=0.0)
    data_warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    expires_after: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    code_version: Mapped[str] = mapped_column(String(60), default="")
    config_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    data_snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    ai_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    extras: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Digest over the immutable fields, verified on read to detect tampering.
    integrity_hash: Mapped[str] = mapped_column(String(64), default="")

    revisions: Mapped[list[SignalRevisionRow]] = relationship(back_populates="signal")


class SignalRevisionRow(Base):
    """Append-only status history for a signal."""

    __tablename__ = "signal_revisions"
    __table_args__ = (
        UniqueConstraint("signal_id", "revision", name="uq_signal_revision"),
        Index("ix_revision_signal", "signal_id", "revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.signal_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text, default="")
    observed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    actor: Mapped[str] = mapped_column(String(60), default="system")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    signal: Mapped[SignalRow] = relationship(back_populates="revisions")


class SignalResearchRevisionRow(Base):
    """Append-only web-research finding attached to a signal.

    Distinct from ``SignalRevisionRow`` (status history, e.g. triggered/
    expired): this table carries what an MCP client's own web research
    contributed after the signal was generated -- an updated thesis, updated
    invalidation conditions, and small, bounded adjustments to the
    already-computed component scores. It never carries a status and never
    touches ``SignalRow.plan`` -- entry, stop, targets and size are
    engine-owned and structurally unreachable from this table's columns.

    Written exclusively through ``claudetrade.signals.research.ResearchLedger
    .append_research_revision``, which validates the target signal exists,
    runs the same rewrite guardrails as ``signals.thesis`` (no unrecognised
    price level, no directive phrase) against the signal's own plan levels,
    clamps every adjustment to ``McpConfig.max_component_adjustment`` and
    rejects unknown component names. A SQLite trigger (installed by the
    migration that creates this table) rejects ``UPDATE`` and ``DELETE``, the
    same append-only guard ``signals``/``signal_revisions`` get from
    migration 002.
    """

    __tablename__ = "signal_research_revisions"
    __table_args__ = (
        UniqueConstraint("signal_id", "revision", name="uq_signal_research_revision"),
        Index("ix_research_revision_signal", "signal_id", "revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.signal_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    actor: Mapped[str] = mapped_column(String(60), default="mcp")
    #: NULL means "unchanged" -- the client submitted no thesis rewrite.
    thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: NULL means "unchanged"; a list of strings when the client submitted one.
    invalidation: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    #: Component name -> signed delta, already clamped to
    #: ``McpConfig.max_component_adjustment`` at write time. Unknown
    #: component names never reach storage -- rejected before the insert.
    score_adjustments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Required, non-empty: why the research changes what it changes.
    rationale: Mapped[str] = mapped_column(Text)
    #: Required, non-empty list of URLs/citations backing the research.
    sources: Mapped[list[Any]] = mapped_column(JSON)
    #: Provider/model/tool metadata about how this revision was produced.
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Digest over (signal_id, revision, thesis, invalidation,
    #: score_adjustments, rationale, sources), verified on read -- see
    #: ``signals.research.research_integrity_payload``.
    integrity_hash: Mapped[str] = mapped_column(String(64), default="")


class DataSnapshotRow(Base):
    """Manifest describing exactly which inputs produced a signal or run.

    Stores hashes and row counts rather than a full data copy; combined with
    the append-only bar tables this is enough to rebuild the inputs and
    reproduce a historical signal.
    """

    __tablename__ = "data_snapshots"

    snapshot_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    symbol_count: Mapped[int] = mapped_column(Integer, default=0)
    bar_count: Mapped[int] = mapped_column(Integer, default=0)
    post_count: Mapped[int] = mapped_column(Integer, default=0)
    providers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------
# Paper trading
# --------------------------------------------------------------------------


class PaperAccountRow(Base):
    __tablename__ = "paper_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(60), unique=True, default="default")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    starting_cash: Mapped[float] = mapped_column(Float, default=100_000.0)
    cash: Mapped[float] = mapped_column(Float, default=100_000.0)
    equity: Mapped[float] = mapped_column(Float, default=100_000.0)
    realised_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    high_water_equity: Mapped[float] = mapped_column(Float, default=100_000.0)
    kill_switch_engaged: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PaperOrderRow(Base):
    __tablename__ = "paper_orders"
    __table_args__ = (Index("ix_paper_order_symbol", "symbol", "created_at"),)

    order_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"), index=True)
    signal_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(10))
    order_type: Mapped[str] = mapped_column(String(20), default="limit")
    quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="working")
    signal_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    filled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PaperTradeRow(Base):
    """Paper position/trade. Open trades carry NULL exit fields."""

    __tablename__ = "paper_trades"
    __table_args__ = (
        Index("ix_paper_trade_symbol", "symbol", "entry_session"),
        Index("ix_paper_trade_open", "exit_session"),
    )

    trade_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"), index=True)
    signal_id: Mapped[str] = mapped_column(String(48), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    strategy: Mapped[str] = mapped_column(String(60), index=True)
    direction: Mapped[str] = mapped_column(String(10))
    signal_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_session: Mapped[dt.date] = mapped_column(Date, index=True)
    entry_price: Mapped[float] = mapped_column(Float)
    shares: Mapped[int] = mapped_column(Integer)
    stop_loss: Mapped[float] = mapped_column(Float)
    original_stop_loss: Mapped[float] = mapped_column(Float)
    targets: Mapped[list[Any]] = mapped_column(JSON, default=list)
    exit_session: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    gross_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    commission_total: Mapped[float] = mapped_column(Float, default=0.0)
    fees_total: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_total: Mapped[float] = mapped_column(Float, default=0.0)
    borrow_cost_total: Mapped[float] = mapped_column(Float, default=0.0)
    mfe_pct: Mapped[float] = mapped_column(Float, default=0.0)
    mae_pct: Mapped[float] = mapped_column(Float, default=0.0)
    mfe_r: Mapped[float] = mapped_column(Float, default=0.0)
    mae_r: Mapped[float] = mapped_column(Float, default=0.0)
    initial_risk_per_share: Mapped[float] = mapped_column(Float, default=0.0)
    outcome: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    r_multiple: Mapped[float] = mapped_column(Float, default=0.0)
    thesis_intact_at_exit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    regime_at_entry: Mapped[str] = mapped_column(String(30), default="unknown")
    sector: Mapped[str] = mapped_column(String(80), default="")
    market_cap_bucket: Mapped[str] = mapped_column(String(20), default="")
    days_to_earnings_at_entry: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence_at_entry: Mapped[float] = mapped_column(Float, default=0.0)
    sentiment_source: Mapped[str] = mapped_column(String(20), default="none")
    adjustments: Mapped[list[Any]] = mapped_column(JSON, default=list)
    notes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    code_version: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PaperEquityCurveRow(Base):
    __tablename__ = "paper_equity_curve"
    __table_args__ = (UniqueConstraint("account_id", "session", name="uq_paper_equity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"), index=True)
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    portfolio_heat_pct: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)


# --------------------------------------------------------------------------
# Backtesting
# --------------------------------------------------------------------------


class BacktestRunRow(Base):
    """One backtest execution and the exact configuration that produced it."""

    __tablename__ = "backtest_runs"

    run_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    label: Mapped[str] = mapped_column(String(120), default="")
    strategies: Mapped[list[Any]] = mapped_column(JSON, default=list)
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    universe_size: Mapped[int] = mapped_column(Integer, default=0)
    initial_capital: Mapped[float] = mapped_column(Float, default=100_000.0)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    code_version: Mapped[str] = mapped_column(String(60), default="")
    data_snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    segment: Mapped[str] = mapped_column(String(30), default="full")  # train|validation|test|full
    parent_run_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)


class BacktestTradeRow(Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (
        Index("ix_bt_trade_run", "run_id", "entry_session"),
        Index("ix_bt_trade_symbol", "run_id", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.run_id"), index=True)
    trade_id: Mapped[str] = mapped_column(String(48))
    signal_id: Mapped[str] = mapped_column(String(48), default="")
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    strategy: Mapped[str] = mapped_column(String(60), index=True)
    direction: Mapped[str] = mapped_column(String(10))
    entry_session: Mapped[dt.date] = mapped_column(Date, index=True)
    entry_price: Mapped[float] = mapped_column(Float)
    shares: Mapped[int] = mapped_column(Integer)
    stop_loss: Mapped[float] = mapped_column(Float)
    targets: Mapped[list[Any]] = mapped_column(JSON, default=list)
    exit_session: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    holding_days: Mapped[int] = mapped_column(Integer, default=0)
    gross_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    net_return_pct: Mapped[float] = mapped_column(Float, default=0.0)
    r_multiple: Mapped[float] = mapped_column(Float, default=0.0)
    outcome: Mapped[str] = mapped_column(String(12), index=True)
    commission_total: Mapped[float] = mapped_column(Float, default=0.0)
    fees_total: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_total: Mapped[float] = mapped_column(Float, default=0.0)
    borrow_cost_total: Mapped[float] = mapped_column(Float, default=0.0)
    mfe_pct: Mapped[float] = mapped_column(Float, default=0.0)
    mae_pct: Mapped[float] = mapped_column(Float, default=0.0)
    mfe_r: Mapped[float] = mapped_column(Float, default=0.0)
    mae_r: Mapped[float] = mapped_column(Float, default=0.0)
    initial_risk_per_share: Mapped[float] = mapped_column(Float, default=0.0)
    regime_at_entry: Mapped[str] = mapped_column(String(30), default="unknown")
    sector: Mapped[str] = mapped_column(String(80), default="", index=True)
    market_cap_bucket: Mapped[str] = mapped_column(String(20), default="")
    days_to_earnings_at_entry: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence_at_entry: Mapped[float] = mapped_column(Float, default=0.0)
    sentiment_source: Mapped[str] = mapped_column(String(20), default="none")
    thesis_intact_at_exit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class BacktestMetricRow(Base):
    """Metric values for a run, optionally sliced by a dimension.

    ``dimension``/``bucket`` hold the segmentation (e.g. ``year``/``2021``,
    ``sector``/``Energy``), with ``dimension='overall'`` for headline numbers.
    """

    __tablename__ = "backtest_metrics"
    __table_args__ = (
        UniqueConstraint("run_id", "dimension", "bucket", name="uq_bt_metric"),
        Index("ix_bt_metric_run", "run_id", "dimension"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.run_id"), index=True)
    dimension: Mapped[str] = mapped_column(String(40), default="overall")
    bucket: Mapped[str] = mapped_column(String(80), default="all")
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    validated: Mapped[bool] = mapped_column(Boolean, default=False)


class BacktestEquityRow(Base):
    __tablename__ = "backtest_equity"
    __table_args__ = (UniqueConstraint("run_id", "session", name="uq_bt_equity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.run_id"), index=True)
    session: Mapped[dt.date] = mapped_column(Date, index=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    exposure_pct: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)


# --------------------------------------------------------------------------
# AI accounting and data quality
# --------------------------------------------------------------------------


class AICallRow(Base):
    """Every AI request, for cost control and auditability."""

    __tablename__ = "ai_calls"
    __table_args__ = (Index("ix_ai_call_time", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    task: Mapped[str] = mapped_column(String(40), index=True)
    prompt_version: Mapped[str] = mapped_column(String(20), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    parsed_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_used: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AICacheRow(Base):
    """Content-addressed cache of AI classifications, to control API cost."""

    __tablename__ = "ai_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    task: Mapped[str] = mapped_column(String(40), index=True)
    provider: Mapped[str] = mapped_column(String(40), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    prompt_version: Mapped[str] = mapped_column(String(20), default="")
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hits: Mapped[int] = mapped_column(Integer, default=0)


class DataQualityRow(Base):
    """Detected data defects. Unresolved errors suppress high-confidence signals."""

    __tablename__ = "data_quality_events"
    __table_args__ = (Index("ix_dq_symbol_time", "symbol", "detected_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    detected_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    severity: Mapped[str] = mapped_column(String(12), index=True)
    category: Mapped[str] = mapped_column(String(50), index=True)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    session: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationRow(Base):
    """Sent notifications, used for cooldown and deduplication."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notify_key_time", "dedupe_key", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    event: Mapped[str] = mapped_column(String(60), index=True)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    channel: Mapped[str] = mapped_column(String(20))
    dedupe_key: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RefreshRunRow(Base):
    """Cross-process record of a data refresh (QA handoff v3, F27).

    Refresh progress used to live only in a per-process dataclass
    (``webapi.refresh_state.RefreshState``), so the CLI, the web API server
    and the MCP server -- each with its own ``Pipeline`` -- were mutually
    blind: whichever process did not start the refresh reported "idle" while
    another was actively writing, and nothing stopped it from starting a
    second concurrent refresh against the same SQLite file. This table is the
    cross-process truth: one row per refresh attempt, written through
    ``db.refresh_state_store`` in its own short transactions.

    ``heartbeat_at`` is the liveness signal -- a "running" row whose heartbeat
    has gone stale (the owning process crashed or was killed) is taken over
    and marked failed by the next acquirer rather than blocking refreshes
    forever. Migration 005 additionally installs a partial unique index
    allowing at most ONE row with ``status='running'``, which is what makes
    acquisition atomic across processes (a losing INSERT gets a constraint
    violation instead of a second concurrent refresh).
    """

    __tablename__ = "refresh_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_point: Mapped[str] = mapped_column(String(20))  # "cli" | "webapi" | "mcp"
    status: Mapped[str] = mapped_column(String(20), index=True)  # running | done | failed
    phase: Mapped[str] = mapped_column(String(40), default="starting")
    symbols_done: Mapped[int] = mapped_column(Integer, default=0)
    symbols_total: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    heartbeat_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ModelVersionRow(Base):
    """Registry of trained ML models and prompt versions used in production."""

    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30))  # "ml" | "prompt"
    name: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    trained_start: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    trained_end: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    artifact_path: Mapped[str | None] = mapped_column(String(400), nullable=True)
    code_version: Mapped[str] = mapped_column(String(60), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=False)


ALL_TABLES = tuple(Base.metadata.tables)
