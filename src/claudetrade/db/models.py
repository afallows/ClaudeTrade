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
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base with a JSON type map suitable for SQLite and Postgres."""

    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


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
