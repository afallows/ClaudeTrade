"""Order execution simulation against the next available bar.

This module decides, given a pending order (or a position that needs managing)
and the *next* bar of daily OHLCV data, whether and at what price it fills.
Daily bars cannot reveal the intrabar path of price, so every rule here is a
documented, deliberately-conservative assumption. Read this before trusting any
number the engine produces:

1. **Stops that gap through their trigger do not fill at the trigger.** If the
   bar's open has already passed the stop level, the fill is the OPEN plus
   additional gap slippage, never the stop price. A backtest that fills every
   stop at its exact level is not modelling a stop order, it is modelling
   free money -- this is the single most consequential assumption in the
   module and it is asserted directly in the verification script.

2. **Intrabar ambiguity is resolved pessimistically by default.** If a bar's
   high-low range reaches both the stop and the (next) target without either
   gapping at the open, daily data cannot tell you which was touched first.
   The default policy assumes the stop fired first (the worse outcome). The
   optimistic policy (target first) exists only for sensitivity testing --
   see ``walkforward.parameter_sensitivity`` -- and must never be the default
   for a headline number, because assuming the favourable order-of-touch is a
   textbook way to manufacture an inflated win rate from data that cannot
   support the claim.

3. **Partial fills are capped by participation.** No single fill may consume
   more than ``CostModelConfig.max_participation_rate`` of the bar's volume.
   Discretionary/protective exits (stop, target, moving-average, pre-earnings)
   that cannot fully fill leave the remainder working at the same terms on
   the next bar -- a stop that "gives up" because the name is illiquid would
   itself be a way to hide a loss. Entry orders behave differently: an entry
   that cannot fully fill has its *remainder cancelled*, not carried forward,
   so a strategy never wakes up holding an unintended, stale entry filled
   days after the signal that produced it.

4. **Forced closes are exempt from the participation cap.** A time stop, a
   delisting close, or an end-of-backtest close must always fully close the
   position -- that is the whole point of those rules -- so they fill their
   entire remaining size against the reference price without regard to the
   bar's volume. This slightly understates the real-world cost of unwinding a
   very illiquid position in one day; that is a known, accepted trade-off in
   favour of never leaving a trade uncounted.

5. **A bar with zero (or missing) volume is treated as halted.** Nothing fills
   against it, of any kind, except a forced close (which by definition does
   not depend on the bar supporting a real fill).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from claudetrade.backtest.costs import CostModel
from claudetrade.config import CostModelConfig
from claudetrade.domain import Bar, Direction, ExitReason

log = logging.getLogger(__name__)

EntryOrderType = Literal["market_on_open", "limit", "stop_entry"]
IntrabarPolicy = Literal["pessimistic", "optimistic"]


# --------------------------------------------------------------------------
# Orders and positions
# --------------------------------------------------------------------------


@dataclass(slots=True)
class EntryOrder:
    """A queued entry, to be evaluated against the next bar.

    ``order_type`` mirrors ``BacktestConfig.entry_reference``:

    * ``market_on_open`` -- fills unconditionally at the next open.
    * ``limit`` -- fills only if the bar's range reaches ``limit_price``.
    * ``stop_entry`` -- a breakout order; fills only once price crosses
      ``stop_price``, honestly (see the gap rule above) if it gaps through.
    """

    symbol: str
    direction: Direction
    shares: int
    order_type: EntryOrderType
    limit_price: float | None = None
    stop_price: float | None = None
    strategy: str = ""
    signal_id: str = ""
    queued_session: dt.date | None = None


@dataclass(slots=True)
class FillResult:
    """Outcome of attempting to fill an ``EntryOrder``."""

    filled: bool
    shares: int
    price: float
    is_partial: bool
    spread_cost_per_share: float
    slippage_per_share: float
    note: str = ""


class PositionView(Protocol):
    """The subset of an open position's state the execution simulator needs.

    Defined here (rather than importing ``backtest.portfolio.Position``) so
    this module has no dependency on the portfolio module; ``portfolio.py``
    imports *this* module, not the other way round.
    """

    direction: Direction
    shares: int
    initial_shares: int
    stop_loss: float
    initial_stop_loss: float
    targets: list[float]
    target_fractions: list[float]
    targets_hit: list[bool]
    trailing_stop_atr: float | None
    time_stop_session: dt.date
    moving_average_exit_period: int | None
    pre_earnings_exit_days: int | None
    next_earnings_date: dt.date | None
    highest_close_since_entry: float
    lowest_close_since_entry: float


@dataclass(slots=True)
class ExitDecision:
    """Outcome of ``ExecutionSimulator.simulate_exit``: fill (all or part)."""

    reason: ExitReason
    price: float
    shares: int
    is_partial: bool
    remaining_shares: int
    spread_cost_per_share: float
    slippage_per_share: float
    target_index: int | None = None
    note: str = ""


# --------------------------------------------------------------------------
# Shared trigger geometry
# --------------------------------------------------------------------------


def _upside_trigger(bar: Bar, level: float) -> tuple[bool, float, bool]:
    """Whether/where a level approached from below was touched this bar.

    Returns ``(triggered, raw_fill_price, gapped)``. Used both for a long's
    profit target and for a short's protective stop / a long breakout entry
    -- anything that fires when price rises through a level.
    """
    if bar.high < level:
        return False, 0.0, False
    if bar.open >= level:
        return True, bar.open, True  # gapped straight past the level at the open
    return True, level, False  # touched intrabar; no evidence of a better price


def _downside_trigger(bar: Bar, level: float) -> tuple[bool, float, bool]:
    """Mirror of ``_upside_trigger`` for a level approached from above."""
    if bar.low > level:
        return False, 0.0, False
    if bar.open <= level:
        return True, bar.open, True
    return True, level, False


# --------------------------------------------------------------------------
# Simulator
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ExecutionSimulator:
    """Turns pending orders / open positions into fills against the next bar."""

    cost_model: CostModel
    cost_config: CostModelConfig
    #: See module docstring point 2. "pessimistic" is the only setting that
    #: should ever back a headline number.
    intrabar_policy: IntrabarPolicy = "pessimistic"

    # --- entries -------------------------------------------------------

    def try_fill_entry(self, order: EntryOrder, bar: Bar) -> FillResult | None:
        """Attempt to fill a queued entry against the next bar.

        Returns ``None`` when the order does not fill at all this bar (limit
        never reached, stop never triggered, or the bar is halted/zero-volume).
        """
        if bar.volume <= 0:
            return None  # halted / no print: nothing can be traded against it

        sign = order.direction.sign
        gapped = False

        if order.order_type == "market_on_open":
            raw_price = bar.open
        elif order.order_type == "limit":
            if order.limit_price is None:
                raise ValueError(f"{order.symbol}: limit entry requires limit_price")
            lp = order.limit_price
            if sign > 0:
                if bar.low > lp:
                    return None
                raw_price = min(bar.open, lp)  # better of limit/open, for a buyer that's the lower
            else:
                if bar.high < lp:
                    return None
                raw_price = max(bar.open, lp)  # for a seller "better" is the higher price
        elif order.order_type == "stop_entry":
            if order.stop_price is None:
                raise ValueError(f"{order.symbol}: stop entry requires stop_price")
            triggered, raw, gapped = (
                _upside_trigger(bar, order.stop_price)
                if sign > 0
                else _downside_trigger(bar, order.stop_price)
            )
            if not triggered:
                return None
            raw_price = raw
        else:  # pragma: no cover - EntryOrderType is exhaustive above
            raise ValueError(f"unknown order_type {order.order_type!r}")

        filled_shares, capped = self._apply_participation_cap(order.shares, bar.volume)
        if filled_shares <= 0:
            return None

        spread = self.cost_model.spread_cost(raw_price)
        slip = self.cost_model.slippage(raw_price, filled_shares, bar.volume, is_gap=gapped)
        fill_price = raw_price + sign * (spread + slip)

        note = ""
        if capped and filled_shares < order.shares:
            note = (
                f"entry partially filled ({filled_shares}/{order.shares} shares); "
                "remainder CANCELLED per policy (see module docstring point 3)"
            )
        return FillResult(
            filled=True,
            shares=filled_shares,
            price=fill_price,
            is_partial=capped and filled_shares < order.shares,
            spread_cost_per_share=spread,
            slippage_per_share=slip,
            note=note,
        )

    # --- exits -----------------------------------------------------------

    def simulate_exit(
        self,
        position: PositionView,
        bar: Bar,
        *,
        force_close: bool = False,
        force_close_reason: ExitReason = ExitReason.END_OF_BACKTEST,
        force_close_price: float | None = None,
        moving_average_value: float | None = None,
    ) -> ExitDecision | None:
        """Decide whether ``position`` is (partly or fully) closed by ``bar``.

        Checks run in this order, and stop once one is met, for one bar:

        1. Forced close (time stop already elapsed at the caller's request,
           delisting, end of backtest) -- see module docstring point 4.
        2. Protective stop / target gapped through at the open (at most one
           of the two can gap, since they sit on opposite sides of entry).
        3. Protective stop / target both reachable intrabar -> resolved by
           ``intrabar_policy`` (pessimistic by default: stop wins).
        4. Protective stop alone.
        5. Target alone.
        6. Time stop (checked every bar; always fires once due -- this is
           what makes "no trade left open indefinitely" true).
        7. Moving-average exit.
        8. Pre-earnings exit.
        """
        if force_close:
            price = force_close_price if force_close_price is not None else bar.close
            return ExitDecision(
                reason=force_close_reason,
                price=price,
                shares=position.shares,
                is_partial=False,
                remaining_shares=0,
                spread_cost_per_share=0.0,
                slippage_per_share=0.0,
                note="forced close: bypasses participation cap and fill mechanics by design",
            )

        if bar.volume <= 0:
            return None  # halted / no print

        sign = position.direction.sign

        stop_hit, stop_raw, stop_gapped = (
            _downside_trigger(bar, position.stop_loss)
            if sign > 0
            else _upside_trigger(bar, position.stop_loss)
        )

        target_idx = next(
            (i for i, hit in enumerate(position.targets_hit) if not hit), None
        )
        target_hit = False
        target_raw = 0.0
        target_gapped = False
        if target_idx is not None:
            level = position.targets[target_idx]
            target_hit, target_raw, target_gapped = (
                _upside_trigger(bar, level) if sign > 0 else _downside_trigger(bar, level)
            )

        # Gaps resolve unambiguously: stop and target sit on opposite sides of
        # entry, so the single opening print can gap through at most one.
        if stop_hit and stop_gapped:
            return self._build_stop_exit(position, bar, stop_raw, gapped=True)
        if target_hit and target_gapped:
            return self._build_target_exit(
                position, bar, target_idx, target_raw, gapped=True  # type: ignore[arg-type]
            )

        if stop_hit and target_hit:
            # Neither gapped, both are inside today's high-low range: daily
            # bars cannot say which was touched first. See point 2 above.
            if self.intrabar_policy == "optimistic":
                return self._build_target_exit(
                    position, bar, target_idx, target_raw, gapped=False  # type: ignore[arg-type]
                )
            return self._build_stop_exit(position, bar, stop_raw, gapped=False)
        if stop_hit:
            return self._build_stop_exit(position, bar, stop_raw, gapped=False)
        if target_hit:
            return self._build_target_exit(
                position, bar, target_idx, target_raw, gapped=False  # type: ignore[arg-type]
            )

        if bar.session >= position.time_stop_session:
            return self._build_discretionary_exit(
                position, bar, ExitReason.TIME_STOP, bar.close, bypass_cap=True
            )

        if position.moving_average_exit_period is not None and moving_average_value is not None:
            below_ma = bar.close < moving_average_value
            adverse_cross = below_ma if sign > 0 else not below_ma
            if adverse_cross:
                return self._build_discretionary_exit(
                    position, bar, ExitReason.MOVING_AVERAGE_EXIT, bar.close, bypass_cap=False
                )

        if (
            position.pre_earnings_exit_days is not None
            and position.next_earnings_date is not None
            and (position.next_earnings_date - bar.session).days <= position.pre_earnings_exit_days
        ):
            return self._build_discretionary_exit(
                position, bar, ExitReason.PRE_EARNINGS_EXIT, bar.close, bypass_cap=False
            )

        return None

    # --- construction helpers ---------------------------------------------

    def _apply_participation_cap(self, shares: int, bar_volume: float) -> tuple[int, bool]:
        """Cap ``shares`` at the configured share of ``bar_volume``.

        Returns ``(filled_shares, was_capped)``.
        """
        if not self.cost_config.enable_partial_fills:
            return shares, False
        cap = int(self.cost_config.max_participation_rate * bar_volume)
        if cap <= 0:
            return 0, True
        if cap < shares:
            return cap, True
        return shares, False

    def _build_stop_exit(
        self, position: PositionView, bar: Bar, raw_price: float, *, gapped: bool
    ) -> ExitDecision:
        if gapped:
            reason = ExitReason.GAP_THROUGH_STOP
        elif position.stop_loss != position.initial_stop_loss:
            reason = ExitReason.TRAILING_STOP
        else:
            reason = ExitReason.STOP_LOSS

        filled_shares, capped = self._apply_participation_cap(position.shares, bar.volume)
        return self._finalise(position, bar, reason, raw_price, filled_shares, capped, gapped)

    def _build_target_exit(
        self,
        position: PositionView,
        bar: Bar,
        target_idx: int,
        raw_price: float,
        *,
        gapped: bool,
    ) -> ExitDecision:
        fraction = (
            position.target_fractions[target_idx]
            if target_idx < len(position.target_fractions)
            else 1.0
        )
        desired = round(position.initial_shares * fraction)
        desired = max(1, min(desired, position.shares))
        filled_shares, capped = self._apply_participation_cap(desired, bar.volume)
        fully_closes = filled_shares >= position.shares
        reason = ExitReason.TARGET if fully_closes else ExitReason.PARTIAL_TARGET
        decision = self._finalise(position, bar, reason, raw_price, filled_shares, capped, gapped)
        decision.target_index = target_idx
        return decision

    def _build_discretionary_exit(
        self,
        position: PositionView,
        bar: Bar,
        reason: ExitReason,
        raw_price: float,
        *,
        bypass_cap: bool,
    ) -> ExitDecision:
        if bypass_cap:
            filled_shares, capped = position.shares, False
        else:
            filled_shares, capped = self._apply_participation_cap(position.shares, bar.volume)
        return self._finalise(position, bar, reason, raw_price, filled_shares, capped, gapped=False)

    def _finalise(
        self,
        position: PositionView,
        bar: Bar,
        reason: ExitReason,
        raw_price: float,
        filled_shares: int,
        capped: bool,
        gapped: bool,
    ) -> ExitDecision:
        sign = position.direction.sign
        spread = self.cost_model.spread_cost(raw_price)
        slip = self.cost_model.slippage(raw_price, filled_shares, bar.volume, is_gap=gapped)
        # Exiting is the mirror of entering: a long's exit is a sale (price
        # received is reduced by costs); a short's exit is a buy-to-cover
        # (price paid is increased by costs).
        fill_price = raw_price - sign * (spread + slip)
        remaining = position.shares - filled_shares
        note = ""
        if capped and remaining > 0:
            note = (
                f"exit partially filled ({filled_shares} of {position.shares} remaining shares); "
                "remainder continues working next session (see module docstring point 3)"
            )
        return ExitDecision(
            reason=reason,
            price=fill_price,
            shares=filled_shares,
            is_partial=remaining > 0,
            remaining_shares=remaining,
            spread_cost_per_share=spread,
            slippage_per_share=slip,
            note=note,
        )


def update_trailing_stop(position: PositionView, atr_value: float) -> float:
    """Recompute an ATR trailing stop from data available at this bar's close.

    Called once per *completed* bar, after that bar's exit decision has
    already been made, using the ATR as of that same bar -- never a future
    one. The stop only ratchets toward the current price (tightening risk);
    it never loosens, so a trailing stop cannot accidentally give back
    protection it had already earned.

    Returns the (possibly unchanged) new stop level; the caller is
    responsible for updating ``highest_close_since_entry`` /
    ``lowest_close_since_entry`` before calling this, and for writing the
    result back onto the position.
    """
    if position.trailing_stop_atr is None or atr_value <= 0:
        return position.stop_loss
    if position.direction.sign > 0:
        candidate = position.highest_close_since_entry - position.trailing_stop_atr * atr_value
        return max(position.stop_loss, candidate)
    candidate = position.lowest_close_since_entry + position.trailing_stop_atr * atr_value
    return min(position.stop_loss, candidate)


__all__ = [
    "EntryOrder",
    "EntryOrderType",
    "ExecutionSimulator",
    "ExitDecision",
    "FillResult",
    "IntrabarPolicy",
    "PositionView",
    "update_trailing_stop",
]
