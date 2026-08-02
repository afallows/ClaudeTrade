"""Analyst-sentiment parsing over TipRanks' ``dataForTicker`` ``overview``.

``providers.market.tipranks.TipRanksProvider`` already fetches (and caches,
one file per symbol, full response body) the ``overview`` object for every
symbol it resolves -- for reference data, a real market cap, and earnings.
That same payload also carries a rich analyst-consensus layer this adapter
previously discarded entirely. This module parses that layer into
``domain.AnalystSnapshot`` **without issuing any additional HTTP call**:
every function here takes the already-fetched-or-cached ``overview`` dict
and reads more of it, exactly the same "call it once, use it for
everything" posture ``TipRanksProvider`` already applies to earnings/caps/
refdata.

**Cache compatibility, checked and confirmed non-issue.** Before writing
this module, ``TipRanksProvider._store_cache_record``/``_load_cache_record``
were read end to end: the on-disk cache record stores the FULL ``overview``
dict verbatim (``record["overview"] = overview``, no field trimming
anywhere in that path), and always has -- nothing in this change alters
that. So every field this module reads was already surviving a cache
round-trip before this module existed; no cache-record versioning or
migration was needed for old cache files to "gain" analyst data, because
the raw analyst fields were sitting unread in every cache file already
written. This is stated explicitly because the task that produced this
module assumed the opposite (a trimmed cache needing a version bump) and
that assumption did not hold once the cache code was actually read.

Every field access below is defensive (``.get()``, guarded type checks,
``isinstance`` gates) -- the same posture as every parser in
``providers.market.tipranks``: an unrecognised or reshaped field must never
be able to break the rest of the snapshot, and a genuinely uncovered symbol
must parse to ``None`` (see :func:`parse_analyst_snapshot`) rather than an
all-``None`` object.

**Fixture cross-references** (``tests/fixtures/tipranks/dataForTicker_INTC
.json`` and ``..._TECK_B.json`` -- both read in full before writing this
module; see each file's own ``_fixture_note``):

* ``overview.consensuses[]`` vs. ``overview.latestRankedConsensus``:
  CONFIRMED to be two different analyst pools on the INTC fixture --
  ``consensuses[0]`` has ``nH=24`` (all analysts covering the stock),
  ``latestRankedConsensus`` has ``nH=23`` (TipRanks' own *ranked* subset,
  i.e. analysts TipRanks has enough of a track record on to score). This
  module uses ``latestRankedConsensus`` for the headline Buy/Hold/Sell
  counts and ``analyst_count`` -- see ``domain.AnalystSnapshot``'s
  docstring for why.
* ``ratingId`` -- CONFIRMED ``1 == "buy"`` from the INTC fixture's Vivek
  Arya row, whose own headline reads "Buy Rating Reaffirmed"; CONFIRMED
  ``3 == "sell"`` from the excluded ``notRankedExperts`` Stocktwits row,
  whose headline reads "...-Bearish". ``2 == "hold"`` follows by
  elimination (no direct headline confirms it, but it is consistent with
  every ``nB``/``nH``/``nS`` count seen and there are only three rating ids
  observed across both fixtures: 1, 2, 3).
* ``actionId`` -- NOT documented anywhere reachable from this adapter.
  Exactly two values are confirmed from headline text: ``3`` on the
  TECK.B fixture's Brian MacArthur row ("upgraded to Outperform from
  Market Perform") -> ``"upgrade"``; ``5`` on three rows across both
  fixtures whose headlines describe an unchanged rating ("Buy Rating
  Reaffirmed", a same-firm price-target raise with no rating change) ->
  ``"reiterate"``. ``8`` appears only on the excluded non-analyst
  Stocktwits row and is left unmapped. No initiate/downgrade value has
  been observed in either fixture; any ``actionId`` this module has not
  confirmed is stored as the raw id with ``action_label=None`` rather than
  guessed.
* ``eTypeId`` -- ``1`` is TipRanks' own professional-analyst type (every
  ``experts[]`` row in both fixtures). ``3`` is the only other value
  observed, on the ``notRankedExperts`` Stocktwits row -- explicitly a
  non-analyst social-media author, per the project owner's instruction.
  This module treats ``1`` as the only inclusion criterion (an allow-list,
  not a ``!= 3`` deny-list) so an unobserved ``eTypeId`` is excluded by
  default rather than assumed to be another analyst flavour.
* ``ptConsensus[]``/``consensuses[]`` in both fixtures each carry exactly
  ONE row (``bench=0``/``bench=1`` respectively) -- real production
  payloads likely carry more (different ``period``s), per the fixtures'
  own "trimmed to representative entries" notes. The selection helpers
  below (``_select_pt_consensus_row``/``_select_latest_consensus_row``)
  are written to prefer the most representative row when several are
  present, but only their single-row fallback path is exercised by the
  committed fixtures.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from claudetrade.domain import AnalystConsensusPoint, AnalystRatingAction, AnalystSnapshot

log = logging.getLogger(__name__)

#: Longest ``consensus_over_time`` series stored per snapshot (most recent
#: points kept, chronological order preserved). Both fixtures carry 5
#: points; this is a generous multiple of that, bounding the stored JSON
#: column's size without starving a real trend chart.
CONSENSUS_OVER_TIME_MAX = 24

#: Longest ``recent_rating_actions`` list stored per snapshot, most-recent
#: first before this cap is applied.
RECENT_RATING_ACTIONS_MAX = 30

#: TipRanks' own 1/2/3 rating code on one ``experts[].ratings[]`` entry.
#: See the module docstring's "fixture cross-references" section for the
#: confirmation evidence behind each value.
_RATING_LABELS: dict[int, str] = {1: "buy", 2: "hold", 3: "sell"}

#: TipRanks' own ``actionId`` code -- UNDOCUMENTED beyond what the module
#: docstring's fixture cross-references confirm. Every other value is
#: deliberately left unmapped.
_ACTION_LABELS: dict[int, str] = {3: "upgrade", 5: "reiterate"}

#: The only ``eTypeId`` this module treats as a real analyst -- an
#: allow-list, not a deny-list of the one non-analyst value observed
#: (``3``). See the module docstring.
_ANALYST_ETYPE_ID = 1


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _select_latest_consensus_row(consensuses: Any) -> dict[str, Any] | None:
    """The ``overview.consensuses[]`` row this snapshot's headline rating
    and consensus-rate come from.

    Prefers a row with both ``isLatest == 1`` and ``bench == 1``; relaxes to
    ``isLatest == 1`` alone, then to the first row present, if the strict
    match is empty. Both fixtures carry exactly one row satisfying the
    strict criterion, so only the strict branch is exercised by a real
    captured payload today -- the relaxed branches are defensive.
    """
    if not isinstance(consensuses, list):
        return None
    rows = [r for r in consensuses if isinstance(r, dict)]
    if not rows:
        return None
    strict = [r for r in rows if r.get("isLatest") == 1 and r.get("bench") == 1]
    if strict:
        return strict[0]
    latest_only = [r for r in rows if r.get("isLatest") == 1]
    if latest_only:
        return latest_only[0]
    return rows[0]


def _select_pt_consensus_row(pt_consensus: Any) -> dict[str, Any] | None:
    """The ``overview.ptConsensus[]`` row this snapshot's price-target
    fields come from.

    Prefers ``bench == 1``, falling back to the first row present. Both
    fixtures carry a single ``bench == 0`` row, so that fallback is the
    path actually exercised by a captured real payload today.
    """
    if not isinstance(pt_consensus, list):
        return None
    rows = [r for r in pt_consensus if isinstance(r, dict)]
    if not rows:
        return None
    bench = [r for r in rows if r.get("bench") == 1]
    return bench[0] if bench else rows[0]


def _parse_consensus_over_time(rows: Any) -> list[AnalystConsensusPoint]:
    """``overview.consensusOverTime[]`` -> a bounded, date-ascending series.

    A row missing its date or any of the three counts contributes nothing
    (never a partially-filled point) -- ``price_target``/``consensus`` are
    the only fields tolerated as missing.
    """
    if not isinstance(rows, list):
        return []
    points: list[AnalystConsensusPoint] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = _parse_date(row.get("date"))
        if date is None:
            continue
        buy = _maybe_int(row.get("buy"))
        hold = _maybe_int(row.get("hold"))
        sell = _maybe_int(row.get("sell"))
        if buy is None or hold is None or sell is None:
            continue
        points.append(
            AnalystConsensusPoint(
                date=date,
                buy=buy,
                hold=hold,
                sell=sell,
                consensus=_maybe_int(row.get("consensus")),
                price_target=_maybe_float(row.get("priceTarget")),
            )
        )
    points.sort(key=lambda p: p.date)
    return points[-CONSENSUS_OVER_TIME_MAX:]


def _expert_stars_and_success(expert: dict[str, Any]) -> tuple[float | None, float | None]:
    """One expert's headline star rating (preferring the ``bench == 1``
    ranking period when more than one is present) and stored success rate."""
    stars: float | None = None
    for ranking in expert.get("rankings") or []:
        if not isinstance(ranking, dict):
            continue
        candidate = _maybe_float(ranking.get("stars"))
        if candidate is None:
            continue
        stars = candidate
        if ranking.get("bench") == 1:
            break
    success_rate = _maybe_float(expert.get("stockSuccessRate"))
    return stars, success_rate


def _parse_rating_actions(experts: Any, not_ranked_experts: Any) -> list[AnalystRatingAction]:
    """Flatten every real analyst's individual rating actions into one
    dated list, most-recent first, capped at ``RECENT_RATING_ACTIONS_MAX``.

    Reads BOTH ``experts[]`` and ``notRankedExperts[]`` -- the latter is
    where the confirmed non-analyst example (a Stocktwits author,
    ``eTypeId=3``) actually lives, so it must be inspected (and filtered),
    not skipped outright. ``_ANALYST_ETYPE_ID`` is the sole inclusion
    filter, applied identically to both pools.
    """
    actions: list[AnalystRatingAction] = []
    pools: list[Any] = [p for p in (experts, not_ranked_experts) if isinstance(p, list)]

    for pool in pools:
        for expert in pool:
            if not isinstance(expert, dict):
                continue
            if expert.get("eTypeId") != _ANALYST_ETYPE_ID:
                continue
            firm = str(expert.get("firm") or "")
            name = str(expert.get("name") or "").strip()
            stars, success_rate = _expert_stars_and_success(expert)
            included = bool(expert.get("includedInConsensus"))

            for rating in expert.get("ratings") or []:
                if not isinstance(rating, dict):
                    continue
                date = _parse_date(rating.get("date"))
                if date is None:
                    continue
                rating_id = _maybe_int(rating.get("ratingId"))
                action_id = _maybe_int(rating.get("actionId"))
                actions.append(
                    AnalystRatingAction(
                        date=date,
                        firm=firm,
                        analyst_name=name,
                        rating_id=rating_id,
                        rating_label=(
                            _RATING_LABELS.get(rating_id) if rating_id is not None else None
                        ),
                        action_id=action_id,
                        action_label=(
                            _ACTION_LABELS.get(action_id) if action_id is not None else None
                        ),
                        price_target=_maybe_float(rating.get("priceTarget")),
                        old_price_target=_maybe_float(rating.get("oldPriceTarget")),
                        analyst_stars=stars,
                        analyst_success_rate=success_rate,
                        included_in_consensus=included,
                    )
                )

    actions.sort(key=lambda a: a.date, reverse=True)
    return actions[:RECENT_RATING_ACTIONS_MAX]


def parse_analyst_snapshot(
    overview: dict[str, Any] | None,
    symbol: str,
    as_of_session: dt.date,
    fetched_at: dt.datetime,
) -> AnalystSnapshot | None:
    """Build one ``AnalystSnapshot`` from a ``dataForTicker`` ``overview``.

    ``overview`` is the SAME dict ``TipRanksProvider._resolve`` already
    fetched (or served from its on-disk cache) for earnings/caps/refdata --
    this function performs no I/O of its own and is safe to call for every
    symbol a refresh already resolved, at zero additional network cost.

    Returns ``None`` when the symbol has no real analyst-coverage layer at
    all (no usable ``consensuses`` row, no ``latestRankedConsensus``, no
    ``ptConsensus`` row, no ``consensusOverTime`` points, and no analyst
    rating actions) -- an empty, all-``None`` snapshot must never be stored
    (the caller's storage layer relies on this). A symbol with genuinely no
    ``overview`` at all (TipRanks has nothing for it --
    ``TipRanksProvider``'s own "unknown"/"prices_only" states) also returns
    ``None`` here, via the same guard.
    """
    if not isinstance(overview, dict):
        return None

    consensus_row = _select_latest_consensus_row(overview.get("consensuses"))
    ranked = overview.get("latestRankedConsensus")
    ranked = ranked if isinstance(ranked, dict) else {}
    pt_row = _select_pt_consensus_row(overview.get("ptConsensus"))
    consensus_over_time = _parse_consensus_over_time(overview.get("consensusOverTime"))
    rating_actions = _parse_rating_actions(
        overview.get("experts"), overview.get("notRankedExperts")
    )

    has_coverage = (
        bool(consensus_row)
        or bool(ranked)
        or bool(pt_row)
        or bool(rating_actions)
        or bool(consensus_over_time)
    )
    if not has_coverage:
        return None

    buy_count = _maybe_int(ranked.get("nB")) or 0
    hold_count = _maybe_int(ranked.get("nH")) or 0
    sell_count = _maybe_int(ranked.get("nS")) or 0

    holding = overview.get("portfolioHoldingData")
    holding = holding if isinstance(holding, dict) else {}
    last_eps = holding.get("lastReportedEps")
    last_eps = last_eps if isinstance(last_eps, dict) else {}
    next_earnings = holding.get("nextEarningsReport")
    next_earnings = next_earnings if isinstance(next_earnings, dict) else {}
    surprise = _maybe_float(last_eps.get("surprise"))

    currency = pt_row.get("priceTargetCurrencyCode") if pt_row else None

    return AnalystSnapshot(
        symbol=symbol,
        as_of_session=as_of_session,
        consensus_rating=_maybe_int(consensus_row.get("rating")) if consensus_row else None,
        buy_count=buy_count,
        hold_count=hold_count,
        sell_count=sell_count,
        consensus_rate=_maybe_float(consensus_row.get("consensusRate")) if consensus_row else None,
        price_target_mean=_maybe_float(pt_row.get("priceTarget")) if pt_row else None,
        price_target_high=_maybe_float(pt_row.get("high")) if pt_row else None,
        price_target_low=_maybe_float(pt_row.get("low")) if pt_row else None,
        price_target_currency=str(currency) if currency else None,
        analyst_count=buy_count + hold_count + sell_count,
        consensus_over_time=consensus_over_time,
        recent_rating_actions=rating_actions,
        last_eps_surprise_pct=(surprise * 100.0 if surprise is not None else None),
        next_earnings_estimate_eps=_maybe_float(next_earnings.get("eps")),
        fetched_at=fetched_at,
    )


__all__ = [
    "CONSENSUS_OVER_TIME_MAX",
    "RECENT_RATING_ACTIONS_MAX",
    "parse_analyst_snapshot",
]
