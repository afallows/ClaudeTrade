"""Reads and diffs over the stored TipRanks institutional-sentiment
snapshots.

Mirrors ``data.analyst`` exactly, one table over: kept apart from
``data.ingest`` (the write path) and ``providers.market
.tipranks_institutional`` (the parser + scoring function):

* :func:`latest_and_previous_snapshots` -- the ONE batched read every caller
  wanting "this symbol's institutional picture" needs (UI, MCP tool). Two
  queries total, however many symbols are asked for -- see its own
  docstring for why this matters (F26).
* :func:`institutional_delta` -- a pure function over two already-loaded
  ``domain.InstitutionalSnapshot`` objects. No database access, no I/O;
  trivially unit-testable and reusable by any caller that already has both
  snapshots in hand.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select

from claudetrade.db.models import InstitutionalSnapshotRow
from claudetrade.db.session import Database
from claudetrade.domain import (
    HedgeFundHolderMove,
    HedgeFundHoldingQuarter,
    InsiderTransaction,
    InsiderTransactionMonth,
    InstitutionalSnapshot,
)


def _row_to_snapshot(row: InstitutionalSnapshotRow) -> InstitutionalSnapshot:
    """Flatten one stored row back into the domain shape -- built while the
    session that loaded it is still open, so no lazy-load can fire later."""
    insider_monthly = [
        InsiderTransactionMonth(
            month=int(m["month"]),
            year=int(m["year"]),
            shares_bought=m.get("shares_bought"),
            insiders_buy_count=int(m.get("insiders_buy_count", 0)),
            shares_sold=m.get("shares_sold"),
            insiders_sell_count=int(m.get("insiders_sell_count", 0)),
            trans_buy_count=int(m.get("trans_buy_count", 0)),
            trans_sell_count=int(m.get("trans_sell_count", 0)),
            trans_buy_amount=m.get("trans_buy_amount"),
            trans_sell_amount=m.get("trans_sell_amount"),
            informative_buy_count=int(m.get("informative_buy_count", 0)),
            informative_sell_count=int(m.get("informative_sell_count", 0)),
            informative_buy_amount=m.get("informative_buy_amount"),
            informative_sell_amount=m.get("informative_sell_amount"),
        )
        for m in (row.insider_monthly or [])
        if isinstance(m, dict) and m.get("month") is not None and m.get("year") is not None
    ]
    recent_insider_transactions = [
        InsiderTransaction(
            name=t.get("name", ""),
            is_officer=bool(t.get("is_officer", False)),
            is_director=bool(t.get("is_director", False)),
            is_ten_percent_owner=bool(t.get("is_ten_percent_owner", False)),
            officer_title=t.get("officer_title"),
            action=t.get("action"),
            operation_description=t.get("operation_description"),
            amount=t.get("amount"),
            number_of_shares=t.get("number_of_shares"),
            r_date=dt.date.fromisoformat(t["r_date"]) if isinstance(t.get("r_date"), str) else None,
            estimated_shares_value=t.get("estimated_shares_value"),
            link=t.get("link"),
        )
        for t in (row.recent_insider_transactions or [])
        if isinstance(t, dict) and t.get("name")
    ]
    holdings_by_quarter = [
        HedgeFundHoldingQuarter(
            date=dt.date.fromisoformat(h["date"]) if isinstance(h.get("date"), str) else h["date"],
            holding_amount=h.get("holding_amount"),
            institution_holding_percentage=h.get("institution_holding_percentage"),
            net_shares_change=h.get("net_shares_change"),
            number_of_shares_bought=h.get("number_of_shares_bought"),
            number_of_shares_sold=h.get("number_of_shares_sold"),
            is_complete=bool(h.get("is_complete", False)),
        )
        for h in (row.hedge_fund_holdings_by_quarter or [])
        if isinstance(h, dict) and h.get("date")
    ]
    notable_holder_moves = [
        HedgeFundHolderMove(
            manager_name=m.get("manager_name", ""),
            institution_name=m.get("institution_name", ""),
            action=m.get("action"),
            effective_date=(
                dt.date.fromisoformat(m["effective_date"])
                if isinstance(m.get("effective_date"), str)
                else None
            ),
            value=m.get("value"),
            change_pct=m.get("change_pct"),
            change_amount=m.get("change_amount"),
            percentage_of_portfolio=m.get("percentage_of_portfolio"),
            stars=m.get("stars"),
            is_active=bool(m.get("is_active", True)),
        )
        for m in (row.notable_holder_moves or [])
        if isinstance(m, dict)
    ]
    return InstitutionalSnapshot(
        symbol=row.symbol,
        as_of_session=row.session,
        insider_monthly=insider_monthly,
        insider_net_3m_usd=row.insider_net_3m_usd,
        insider_net_3m_usd_vendor=row.insider_net_3m_usd_vendor,
        insider_confidence_stock_score=row.insider_confidence_stock_score,
        insider_confidence_sector_score=row.insider_confidence_sector_score,
        insider_confidence_raw_score=row.insider_confidence_raw_score,
        num_of_insiders=row.num_of_insiders,
        recent_insider_transactions=recent_insider_transactions,
        hedge_fund_sentiment=row.hedge_fund_sentiment,
        hedge_fund_trend_action=row.hedge_fund_trend_action,
        hedge_fund_trend_value=row.hedge_fund_trend_value,
        hedge_fund_holdings_by_quarter=holdings_by_quarter,
        notable_holder_moves=notable_holder_moves,
        market_cap_usd=row.market_cap_usd,
        fetched_at=row.fetched_at,
    )


def snapshot_to_row_fields(
    snapshot: InstitutionalSnapshot, score_result
) -> dict[str, object]:
    """The column values ``data.ingest.DataIngestor
    .ingest_institutional_snapshots`` writes onto an
    ``InstitutionalSnapshotRow`` -- the write-side mirror of
    :func:`_row_to_snapshot`, kept here so the two stay in lockstep instead
    of drifting if a field is added to one but not the other.

    ``score_result`` is the
    ``tipranks_institutional.institutional_score(snapshot, session_date)``
    output the caller already computed -- passed in rather than recomputed
    here, so the score stored on the row and the score a caller might log
    or act on in the same ingest cycle are guaranteed to be the exact same
    computation, not two separate calls that could theoretically observe
    different ``as_of`` clock values.
    """
    return {
        "insider_monthly": [
            {
                "month": m.month,
                "year": m.year,
                "shares_bought": m.shares_bought,
                "insiders_buy_count": m.insiders_buy_count,
                "shares_sold": m.shares_sold,
                "insiders_sell_count": m.insiders_sell_count,
                "trans_buy_count": m.trans_buy_count,
                "trans_sell_count": m.trans_sell_count,
                "trans_buy_amount": m.trans_buy_amount,
                "trans_sell_amount": m.trans_sell_amount,
                "informative_buy_count": m.informative_buy_count,
                "informative_sell_count": m.informative_sell_count,
                "informative_buy_amount": m.informative_buy_amount,
                "informative_sell_amount": m.informative_sell_amount,
            }
            for m in snapshot.insider_monthly
        ],
        "insider_net_3m_usd": snapshot.insider_net_3m_usd,
        "insider_net_3m_usd_vendor": snapshot.insider_net_3m_usd_vendor,
        "insider_confidence_stock_score": snapshot.insider_confidence_stock_score,
        "insider_confidence_sector_score": snapshot.insider_confidence_sector_score,
        "insider_confidence_raw_score": snapshot.insider_confidence_raw_score,
        "num_of_insiders": snapshot.num_of_insiders,
        "recent_insider_transactions": [
            {
                "name": t.name,
                "is_officer": t.is_officer,
                "is_director": t.is_director,
                "is_ten_percent_owner": t.is_ten_percent_owner,
                "officer_title": t.officer_title,
                "action": t.action,
                "operation_description": t.operation_description,
                "amount": t.amount,
                "number_of_shares": t.number_of_shares,
                "r_date": t.r_date.isoformat() if t.r_date else None,
                "estimated_shares_value": t.estimated_shares_value,
                "link": t.link,
            }
            for t in snapshot.recent_insider_transactions
        ],
        "hedge_fund_sentiment": snapshot.hedge_fund_sentiment,
        "hedge_fund_trend_action": snapshot.hedge_fund_trend_action,
        "hedge_fund_trend_value": snapshot.hedge_fund_trend_value,
        "hedge_fund_holdings_by_quarter": [
            {
                "date": h.date.isoformat(),
                "holding_amount": h.holding_amount,
                "institution_holding_percentage": h.institution_holding_percentage,
                "net_shares_change": h.net_shares_change,
                "number_of_shares_bought": h.number_of_shares_bought,
                "number_of_shares_sold": h.number_of_shares_sold,
                "is_complete": h.is_complete,
            }
            for h in snapshot.hedge_fund_holdings_by_quarter
        ],
        "notable_holder_moves": [
            {
                "manager_name": m.manager_name,
                "institution_name": m.institution_name,
                "action": m.action,
                "effective_date": m.effective_date.isoformat() if m.effective_date else None,
                "value": m.value,
                "change_pct": m.change_pct,
                "change_amount": m.change_amount,
                "percentage_of_portfolio": m.percentage_of_portfolio,
                "stars": m.stars,
                "is_active": m.is_active,
            }
            for m in snapshot.notable_holder_moves
        ],
        "market_cap_usd": snapshot.market_cap_usd,
        "score": score_result.score,
        "insider_subscore": score_result.insider_subscore,
        "insider_weight_applied": score_result.insider_weight_applied,
        "insider_age_days": score_result.insider_age_days,
        "hedge_fund_subscore": score_result.hedge_fund_subscore,
        "hedge_fund_weight_applied": score_result.hedge_fund_weight_applied,
        "hedge_fund_age_days": score_result.hedge_fund_age_days,
        "fetched_at": snapshot.fetched_at,
    }


def latest_and_previous_snapshots(
    db: Database, symbols: Sequence[str]
) -> dict[str, tuple[InstitutionalSnapshot | None, InstitutionalSnapshot | None]]:
    """The latest and second-latest stored snapshot per symbol, in TWO
    queries total -- never one query per symbol (F26 discipline; see
    ``data.analyst.latest_and_previous_snapshots``, which this mirrors
    exactly one table over).

    Returns every symbol in ``symbols`` as a key. A symbol with no stored
    snapshot at all maps to ``(None, None)``; one with exactly one stored
    session maps to ``(latest, None)``.
    """
    ids = list(dict.fromkeys(symbols))  # de-dup, preserve order
    out: dict[str, tuple[InstitutionalSnapshot | None, InstitutionalSnapshot | None]] = dict.fromkeys(
        ids, (None, None)
    )
    if not ids:
        return out

    with db.read_session() as session:
        latest_sub = (
            select(
                InstitutionalSnapshotRow.symbol.label("symbol"),
                func.max(InstitutionalSnapshotRow.session).label("max_session"),
            )
            .where(InstitutionalSnapshotRow.symbol.in_(ids))
            .group_by(InstitutionalSnapshotRow.symbol)
            .subquery()
        )
        latest_rows = (
            session.execute(
                select(InstitutionalSnapshotRow).join(
                    latest_sub,
                    (latest_sub.c.symbol == InstitutionalSnapshotRow.symbol)
                    & (latest_sub.c.max_session == InstitutionalSnapshotRow.session),
                )
            )
            .scalars()
            .all()
        )
        latest_by_symbol = {row.symbol: _row_to_snapshot(row) for row in latest_rows}

        previous_sub = (
            select(
                InstitutionalSnapshotRow.symbol.label("symbol"),
                func.max(InstitutionalSnapshotRow.session).label("prev_session"),
            )
            .join(
                latest_sub,
                (latest_sub.c.symbol == InstitutionalSnapshotRow.symbol)
                & (InstitutionalSnapshotRow.session < latest_sub.c.max_session),
            )
            .group_by(InstitutionalSnapshotRow.symbol)
            .subquery()
        )
        previous_rows = (
            session.execute(
                select(InstitutionalSnapshotRow).join(
                    previous_sub,
                    (previous_sub.c.symbol == InstitutionalSnapshotRow.symbol)
                    & (previous_sub.c.prev_session == InstitutionalSnapshotRow.session),
                )
            )
            .scalars()
            .all()
        )
        previous_by_symbol = {row.symbol: _row_to_snapshot(row) for row in previous_rows}

    for symbol in ids:
        out[symbol] = (latest_by_symbol.get(symbol), previous_by_symbol.get(symbol))
    return out


@dataclass(slots=True, frozen=True)
class InstitutionalDelta:
    """What changed between two consecutive stored ``InstitutionalSnapshot``
    rows for one symbol -- a pure, read-time comparison, never stored
    itself.

    Every ``*_change`` field is ``None`` when either side of the comparison
    is missing the underlying value -- never a fabricated zero standing in
    for "unknown", the same absent-vs-neutral discipline
    ``data.analyst.AnalystDelta`` already follows.
    """

    symbol: str
    current_session: dt.date
    previous_session: dt.date | None
    has_previous: bool
    score_change: float | None = None
    net_flow_change: float | None = None
    hedge_fund_sentiment_change: float | None = None
    #: Notable holder moves from the CURRENT snapshot's own
    #: ``notable_holder_moves`` whose ``effective_date`` is strictly after
    #: ``previous_session`` -- moves that could not have been reflected in
    #: the previous snapshot. Empty (not "all of current's moves") when
    #: there is no previous snapshot at all, same rationale as
    #: ``AnalystDelta.new_rating_actions``.
    new_holder_moves: list[HedgeFundHolderMove] = field(default_factory=list)
    #: Individual insider transactions from the CURRENT snapshot's own
    #: ``recent_insider_transactions`` whose ``r_date`` is strictly after
    #: ``previous_session``.
    new_insider_transactions: list[InsiderTransaction] = field(default_factory=list)


def institutional_delta(
    current: InstitutionalSnapshot, previous: InstitutionalSnapshot | None
) -> InstitutionalDelta:
    """Compare ``current`` against ``previous`` (the prior stored session,
    or ``None`` when this is the first snapshot this installation has ever
    stored for the symbol). Pure function -- no I/O, no database access.

    ``score_change``/``net_flow_change``/``hedge_fund_sentiment_change``
    compare the RAW stored fields (``insider_net_3m_usd``,
    ``hedge_fund_sentiment``), not a re-invocation of
    ``institutional_score`` -- the score itself is computed and stored at
    ingest time (see ``data.institutional.snapshot_to_row_fields``), but
    this pure delta function has no ``score`` field on the domain object to
    diff directly, so callers wanting a score delta pass the two stored
    rows' own ``score`` columns through the ``score_change`` argument at
    the call site... this function instead accepts the two RAW snapshots
    only and leaves the score comparison to whichever caller already has
    both stored rows' ``score`` columns in hand (see
    ``mcp_server.get_institutional_sentiment`` for the pattern).
    """
    if previous is None:
        return InstitutionalDelta(
            symbol=current.symbol,
            current_session=current.as_of_session,
            previous_session=None,
            has_previous=False,
        )

    net_flow_change: float | None = None
    if current.insider_net_3m_usd is not None and previous.insider_net_3m_usd is not None:
        net_flow_change = current.insider_net_3m_usd - previous.insider_net_3m_usd

    hf_sentiment_change: float | None = None
    if current.hedge_fund_sentiment is not None and previous.hedge_fund_sentiment is not None:
        hf_sentiment_change = current.hedge_fund_sentiment - previous.hedge_fund_sentiment

    new_holder_moves = [
        move
        for move in current.notable_holder_moves
        if move.effective_date is not None and move.effective_date > previous.as_of_session
    ]
    new_insider_transactions = [
        txn
        for txn in current.recent_insider_transactions
        if txn.r_date is not None and txn.r_date > previous.as_of_session
    ]

    return InstitutionalDelta(
        symbol=current.symbol,
        current_session=current.as_of_session,
        previous_session=previous.as_of_session,
        has_previous=True,
        net_flow_change=net_flow_change,
        hedge_fund_sentiment_change=hf_sentiment_change,
        new_holder_moves=new_holder_moves,
        new_insider_transactions=new_insider_transactions,
    )


__all__ = [
    "InstitutionalDelta",
    "institutional_delta",
    "latest_and_previous_snapshots",
    "snapshot_to_row_fields",
]
