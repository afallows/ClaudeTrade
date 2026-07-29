"""Data-quality detection.

Bad data does not announce itself; it shows up as an implausibly good backtest.
This module looks for the specific defects that corrupt this application's
results, records them in ``data_quality_events``, and -- importantly -- exposes
``suppresses_high_confidence`` so the signal engine can refuse to emit a
confident signal that rests on broken inputs.

The checks are intentionally conservative. A false positive costs one skipped
candidate; a false negative costs a wrong trade.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select

from claudetrade.config import AppConfig
from claudetrade.db.models import DataQualityRow
from claudetrade.db.session import Database
from claudetrade.domain import (
    Bar,
    CorporateAction,
    DataQualityIssue,
    DataQualitySeverity,
    EarningsEvent,
    SocialPost,
)
from claudetrade.logging_setup import get_logger
from claudetrade.utils.timeutils import is_trading_day, trading_day_range, utc_now

log = get_logger(__name__)

#: A single-session move beyond this is either a real event or bad data. Either
#: way it deserves a look before it drives a position.
EXTREME_MOVE_PCT = 50.0
#: Categories severe enough to block a high-confidence signal.
BLOCKING_CATEGORIES = frozenset(
    {"missing_bars", "stale_data", "abnormal_price", "conflicting_earnings", "zero_volume"}
)


@dataclass(slots=True)
class QualityReport:
    """Findings for one symbol (or one batch)."""

    issues: list[DataQualityIssue] = field(default_factory=list)

    def add(
        self,
        severity: DataQualitySeverity,
        category: str,
        message: str,
        *,
        symbol: str | None = None,
        session: dt.date | None = None,
        **detail: object,
    ) -> None:
        self.issues.append(
            DataQualityIssue(
                detected_at=utc_now(),
                severity=severity,
                category=category,
                symbol=symbol,
                session=session,
                message=message,
                detail=dict(detail),
            )
        )

    @property
    def errors(self) -> list[DataQualityIssue]:
        return [i for i in self.issues if i.severity is DataQualitySeverity.ERROR]

    @property
    def warnings(self) -> list[DataQualityIssue]:
        return [i for i in self.issues if i.severity is DataQualitySeverity.WARNING]

    def messages_for(self, symbol: str) -> list[str]:
        return [i.message for i in self.issues if i.symbol == symbol]

    def suppresses_high_confidence(self, symbol: str) -> bool:
        """Whether this symbol's defects should cap signal confidence.

        Called by the context builder; the resulting warnings flow into
        ``StrategyContext.data_warnings`` and reduce the confidence score.
        """
        return any(
            issue.symbol == symbol
            and issue.severity is DataQualitySeverity.ERROR
            and issue.category in BLOCKING_CATEGORIES
            for issue in self.issues
        )

    def extend(self, other: QualityReport) -> None:
        self.issues.extend(other.issues)


class DataQualityChecker:
    """Runs the quality checks and persists findings."""

    def __init__(self, config: AppConfig, db: Database | None = None):
        self.config = config
        self.db = db

    # --- price data -------------------------------------------------------

    def check_bars(
        self,
        symbol: str,
        bars: list[Bar],
        *,
        expected_start: dt.date | None = None,
        expected_end: dt.date | None = None,
        report: QualityReport | None = None,
    ) -> QualityReport:
        """Validate a symbol's daily bar series."""
        report = report or QualityReport()
        if not bars:
            report.add(
                DataQualitySeverity.ERROR,
                "missing_bars",
                f"{symbol}: no price bars returned",
                symbol=symbol,
            )
            return report

        ordered = sorted(bars, key=lambda b: b.session)
        if [b.session for b in ordered] != [b.session for b in bars]:
            report.add(
                DataQualitySeverity.WARNING,
                "unordered_bars",
                f"{symbol}: bars were not in chronological order and have been sorted",
                symbol=symbol,
            )

        # Duplicates would double-count a session in every rolling window.
        counts = Counter(b.session for b in ordered)
        for session, count in counts.items():
            if count > 1:
                report.add(
                    DataQualitySeverity.ERROR,
                    "duplicate_bars",
                    f"{symbol}: {count} bars for {session}",
                    symbol=symbol,
                    session=session,
                )

        for bar in ordered:
            self._check_single_bar(symbol, bar, report)

        # Gaps against the exchange calendar.
        start = expected_start or ordered[0].session
        end = expected_end or ordered[-1].session
        expected = set(trading_day_range(start, end))
        present = {b.session for b in ordered}
        missing = sorted(expected - present)
        if missing:
            severity = (
                DataQualitySeverity.ERROR
                if len(missing) > max(3, 0.02 * len(expected))
                else DataQualitySeverity.WARNING
            )
            report.add(
                severity,
                "missing_bars",
                f"{symbol}: {len(missing)} trading sessions missing between {start} and {end}",
                symbol=symbol,
                first_missing=str(missing[0]),
                last_missing=str(missing[-1]),
                missing_count=len(missing),
            )

        unexpected = sorted(s for s in present if not is_trading_day(s))
        if unexpected:
            report.add(
                DataQualitySeverity.WARNING,
                "non_trading_bars",
                f"{symbol}: {len(unexpected)} bars fall on non-trading days "
                "(possible timezone or calendar error)",
                symbol=symbol,
                examples=[str(s) for s in unexpected[:5]],
            )

        self._check_extreme_moves(symbol, ordered, report)
        return report

    def _check_single_bar(self, symbol: str, bar: Bar, report: QualityReport) -> None:
        if bar.high < bar.low:
            report.add(
                DataQualitySeverity.ERROR,
                "abnormal_price",
                f"{symbol} {bar.session}: high {bar.high} below low {bar.low}",
                symbol=symbol,
                session=bar.session,
            )
        if not (bar.low <= bar.open <= bar.high):
            report.add(
                DataQualitySeverity.ERROR,
                "abnormal_price",
                f"{symbol} {bar.session}: open {bar.open} outside the {bar.low}-{bar.high} range",
                symbol=symbol,
                session=bar.session,
            )
        if not (bar.low <= bar.close <= bar.high):
            report.add(
                DataQualitySeverity.ERROR,
                "abnormal_price",
                f"{symbol} {bar.session}: close {bar.close} outside the bar range",
                symbol=symbol,
                session=bar.session,
            )
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            report.add(
                DataQualitySeverity.ERROR,
                "abnormal_price",
                f"{symbol} {bar.session}: non-positive price",
                symbol=symbol,
                session=bar.session,
            )
        if bar.volume < 0:
            report.add(
                DataQualitySeverity.ERROR,
                "abnormal_price",
                f"{symbol} {bar.session}: negative volume",
                symbol=symbol,
                session=bar.session,
            )
        elif bar.volume == 0:
            # A halted or untraded session cannot be filled in a simulation.
            report.add(
                DataQualitySeverity.WARNING,
                "zero_volume",
                f"{symbol} {bar.session}: zero volume (halt, holiday or missing data)",
                symbol=symbol,
                session=bar.session,
            )

    def _check_extreme_moves(
        self, symbol: str, bars: list[Bar], report: QualityReport
    ) -> None:
        """Flag single-session moves large enough to suggest an unadjusted split."""
        for prev, cur in zip(bars, bars[1:], strict=False):
            if prev.close <= 0:
                continue
            change = 100.0 * (cur.close - prev.close) / prev.close
            if abs(change) < EXTREME_MOVE_PCT:
                continue
            # A ratio close to a common split factor is the giveaway.
            ratio = prev.close / cur.close if cur.close > 0 else 0.0
            looks_like_split = any(abs(ratio - f) < 0.06 for f in (2.0, 3.0, 4.0, 5.0, 10.0, 0.5))
            report.add(
                DataQualitySeverity.ERROR if looks_like_split else DataQualitySeverity.WARNING,
                "abnormal_price",
                f"{symbol} {cur.session}: {change:+.1f}% single-session move"
                + (" -- consistent with an unadjusted split" if looks_like_split else ""),
                symbol=symbol,
                session=cur.session,
                change_pct=round(change, 2),
                suspected_split=looks_like_split,
            )

    def check_staleness(
        self,
        symbol: str,
        bars: list[Bar],
        as_of: dt.date,
        *,
        report: QualityReport | None = None,
    ) -> QualityReport:
        """Whether the latest bar is recent enough to act on."""
        report = report or QualityReport()
        if not bars:
            return report
        latest = max(b.session for b in bars)
        age_hours = (as_of - latest).days * 24.0
        limit = self.config.market_data.stale_after_hours
        if age_hours > limit:
            severity = (
                DataQualitySeverity.ERROR if age_hours > limit * 3 else DataQualitySeverity.WARNING
            )
            report.add(
                severity,
                "stale_data",
                f"{symbol}: latest bar is {latest}, {age_hours:.0f}h old "
                f"(limit {limit:.0f}h)",
                symbol=symbol,
                session=latest,
                age_hours=age_hours,
            )
        return report

    # --- corporate actions and earnings ------------------------------------

    def check_corporate_actions(
        self,
        symbol: str,
        actions: list[CorporateAction],
        bars: list[Bar],
        *,
        report: QualityReport | None = None,
    ) -> QualityReport:
        """Confirm that splits are reflected in the adjusted series."""
        report = report or QualityReport()
        by_session = {b.session: b for b in bars}
        for action in actions:
            if action.kind == "split" and action.ratio:
                bar = by_session.get(action.session)
                if bar is None:
                    report.add(
                        DataQualitySeverity.WARNING,
                        "split_without_bar",
                        f"{symbol}: split on {action.session} has no matching price bar",
                        symbol=symbol,
                        session=action.session,
                    )
                elif bar.adj_close is None:
                    report.add(
                        DataQualitySeverity.ERROR,
                        "unadjusted_split",
                        f"{symbol}: split on {action.session} but no adjusted close is available; "
                        "indicators computed on raw prices will be wrong",
                        symbol=symbol,
                        session=action.session,
                    )
            if action.kind == "symbol_change":
                report.add(
                    DataQualitySeverity.INFO,
                    "symbol_change",
                    f"{symbol}: symbol change on {action.session} ({action.detail})",
                    symbol=symbol,
                    session=action.session,
                )
            if action.kind == "delisting":
                report.add(
                    DataQualitySeverity.INFO,
                    "delisting",
                    f"{symbol}: delisted on {action.session}; retained for unbiased backtesting",
                    symbol=symbol,
                    session=action.session,
                )
        return report

    def check_earnings(
        self,
        symbol: str,
        events: list[EarningsEvent],
        *,
        report: QualityReport | None = None,
    ) -> QualityReport:
        """Detect conflicting or implausible earnings dates."""
        report = report or QualityReport()
        if not events:
            return report

        # Two *different* dates for what is evidently the same quarter.
        by_date = sorted(events, key=lambda e: e.report_date)
        for earlier, later in zip(by_date, by_date[1:], strict=False):
            gap = (later.report_date - earlier.report_date).days
            if gap == 0 and earlier.source != later.source:
                continue
            if 0 < gap < 30:
                report.add(
                    DataQualitySeverity.ERROR,
                    "conflicting_earnings",
                    f"{symbol}: earnings dates {earlier.report_date} and {later.report_date} are "
                    f"only {gap} days apart -- the calendar is inconsistent",
                    symbol=symbol,
                    session=later.report_date,
                    sources=[earlier.source, later.source],
                )

        for event in events:
            if not event.confirmed:
                report.add(
                    DataQualitySeverity.INFO,
                    "estimated_earnings_date",
                    f"{symbol}: {event.report_date} is an estimate, not a confirmed date",
                    symbol=symbol,
                    session=event.report_date,
                )
            if event.as_of is None:
                # Without a knowledge date the backtester cannot prove the entry
                # was not visible before it was announced.
                report.add(
                    DataQualitySeverity.WARNING,
                    "earnings_missing_as_of",
                    f"{symbol}: earnings entry for {event.report_date} has no knowledge date; "
                    "it cannot be used in a leak-free backtest",
                    symbol=symbol,
                    session=event.report_date,
                )
        return report

    # --- social data --------------------------------------------------------

    def check_social(
        self,
        symbol: str,
        posts: list[SocialPost],
        *,
        report: QualityReport | None = None,
    ) -> QualityReport:
        """Flag thin or suspicious social samples."""
        report = report or QualityReport()
        minimum = self.config.sentiment.min_posts_for_signal
        if len(posts) < minimum:
            report.add(
                DataQualitySeverity.WARNING,
                "low_sentiment_sample",
                f"{symbol}: only {len(posts)} posts, below the {minimum} minimum for a "
                "sentiment-driven signal",
                symbol=symbol,
                post_count=len(posts),
            )
        if posts:
            hashes = Counter(p.text_hash for p in posts if p.text_hash)
            duplicated = sum(c for c in hashes.values() if c > 1)
            ratio = duplicated / len(posts)
            if ratio > self.config.sentiment.duplicate_ratio_alert:
                report.add(
                    DataQualitySeverity.WARNING,
                    "duplicate_social_content",
                    f"{symbol}: {ratio:.0%} of posts are duplicates or near-duplicates",
                    symbol=symbol,
                    duplicate_ratio=round(ratio, 3),
                )
            injected = [p for p in posts if p.injection_risk > 0.4]
            if injected:
                report.add(
                    DataQualitySeverity.WARNING,
                    "prompt_injection_content",
                    f"{symbol}: {len(injected)} posts contain instruction-like sequences and "
                    "were withheld from AI classification",
                    symbol=symbol,
                    count=len(injected),
                )
        return report

    def check_provider_gap(
        self,
        provider: str,
        expected: int,
        received: int,
        *,
        report: QualityReport | None = None,
    ) -> QualityReport:
        """Detect a sudden shortfall in provider responses."""
        report = report or QualityReport()
        if expected <= 0:
            return report
        ratio = received / expected
        if ratio < 0.5:
            report.add(
                DataQualitySeverity.ERROR,
                "api_data_gap",
                f"{provider}: returned data for only {received}/{expected} requested symbols",
                provider=provider,
                ratio=round(ratio, 3),
            )
        elif ratio < 0.9:
            report.add(
                DataQualitySeverity.WARNING,
                "api_data_gap",
                f"{provider}: partial response, {received}/{expected} symbols",
                provider=provider,
                ratio=round(ratio, 3),
            )
        return report

    # --- persistence ---------------------------------------------------------

    def persist(self, report: QualityReport) -> int:
        """Write findings to ``data_quality_events``. Returns rows written."""
        if self.db is None or not report.issues:
            return 0
        with self.db.session() as session:
            for issue in report.issues:
                session.add(
                    DataQualityRow(
                        detected_at=issue.detected_at,
                        severity=issue.severity.value,
                        category=issue.category,
                        symbol=issue.symbol,
                        session=issue.session,
                        message=issue.message,
                        detail=issue.detail,
                    )
                )
        log.info("recorded %d data-quality findings", len(report.issues))
        return len(report.issues)

    def open_issues(self, *, limit: int = 200) -> list[DataQualityRow]:
        """Unresolved findings, newest first, for the dashboard."""
        if self.db is None:
            return []
        with self.db.read_session() as session:
            return list(
                session.execute(
                    select(DataQualityRow)
                    .where(DataQualityRow.resolved.is_(False))
                    .order_by(DataQualityRow.detected_at.desc())
                    .limit(limit)
                ).scalars()
            )
