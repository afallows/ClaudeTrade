"""The broker execution boundary (ADR-0007 Decision 4).

Every subsystem that needs to act on a market -- the paper runner today, a
live Alpaca adapter later -- talks to a ``BrokerProvider``, never to a
concrete broker class directly. That is what lets the same strategy/signal
code drive paper and (eventually) live trading unmodified, and what makes it
possible to test the risk guard once instead of once per adapter.

Two design choices are deliberate, not incidental:

* **Reuse the domain vocabulary.** ``OrderRequest`` carries a full ``Signal``
  rather than reinventing symbol/side/qty fields, because every
  implementation we have (and the live one we will eventually write) prices
  and sizes off the same entry zone, stop and targets the signal already
  carries -- a second "order" shape would just be a second source of truth
  for the same numbers. Positions are reported as ``Trade`` (already the
  ledger's own record of a position), not a bespoke ``Position`` type.
  ``OrderStatus``/``ACTIVE_STATUSES`` live in ``claudetrade.domain`` so every
  adapter reports the same closed set of lifecycle states.

* **The risk guard lives at the boundary, not in each adapter.** ``submit_order``,
  ``cancel_order`` and ``modify_order`` are concrete template methods that run
  a guard check *before* handing off to an implementation's ``_submit_order``
  / ``_cancel_order`` / ``_modify_order``. An adapter cannot forget to check
  the kill switch or live-trading authorisation because it never gets the
  chance to skip the check -- it only ever sees the call after the guard has
  already passed. This mirrors how ``claudetrade.risk.limits.check_new_position``
  reads ``config.risk.kill_switch_engaged`` / ``config.trading.kill_switch_engaged``
  before any position-sizing math runs; the guard functions below read the
  same two flags for the same reason, rather than calling
  ``check_new_position`` itself, because that function's signature is shaped
  for *sizing* a new position (notional, dollar risk, sector...) and does not
  fit a cancel or modify call that carries none of those.

Shape studied (read-only, ideas only -- GPL/MIT conflict, no code copied) from
``lumibot/brokers/broker.py:893-1038`` and ``example_broker.py``: the
submit/cancel/modify/balances/positions/orders surface and the
public-wrapper-calls-private-abstract pattern. The MIT-licensed
``reddit-options-trader-rot-/src/rot/brokers/base.py`` (Mattbusel/ROT)
confirmed the "typed dataclasses for the order/account shapes, ABC for the
adapter" split is a reasonable minimum; adapted in spirit, not copied.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from claudetrade.config import AppConfig
from claudetrade.domain import Bar, Direction, Fill, OrderStatus, Signal, Trade

# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class TradingHaltedError(RuntimeError):
    """An order-affecting broker call was refused at the execution boundary.

    Raised by ``BrokerProvider``'s ``submit_order``/``cancel_order``/
    ``modify_order`` wrappers before any concrete adapter runs -- see the
    module docstring. Distinct from a broker *rejecting* an order (that is a
    normal outcome carried in ``BrokerOrder.reasons``); this means the call
    was never allowed to reach the adapter at all.
    """


class NotConfiguredError(RuntimeError):
    """A broker adapter exists but has no working venue/credentials wired up.

    Raised by stub or not-yet-implemented adapters (``brokers.null_live``)
    from inside their own methods, i.e. *after* the guard has already passed
    -- it answers "can this adapter actually do anything?", which is a
    different question from "is this call allowed at all?".
    """


class BrokerOrderError(RuntimeError):
    """An order lookup or state transition was invalid (unknown id, terminal state, ...)."""


# --------------------------------------------------------------------------
# Typed shapes
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class OrderRequest:
    """A request to act on a ``Signal``, addressed to whichever broker is active.

    ``next_bar``/``marks`` are execution context a *simulated* broker needs to
    decide whether and where a limit would have filled; a live adapter prices
    off the real market and ignores them. Keeping them optional here (rather
    than giving paper and live brokers different request types) is what lets
    one ``BrokerProvider.submit_order`` signature serve both.
    """

    signal: Signal
    next_bar: Bar | None = None
    marks: dict[str, float] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.signal.symbol

    @property
    def direction(self) -> Direction:
        return self.signal.direction

    @property
    def shares(self) -> int:
        return self.signal.plan.shares


@dataclass(slots=True)
class BrokerOrder:
    """A broker's view of one order, in the closed ``OrderStatus`` vocabulary.

    ``reasons`` carries rejection or cancellation explanations -- rejection is
    a normal outcome, not an exception, matching ``PaperOrderResult`` today.
    """

    order_id: str
    symbol: str
    direction: Direction
    status: OrderStatus
    requested_shares: int
    filled_shares: int = 0
    average_fill_price: float | None = None
    fills: list[Fill] = field(default_factory=list)
    submitted_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        """Whether this order can still fill, be cancelled, or be modified."""
        from claudetrade.domain import ACTIVE_STATUSES

        return self.status in ACTIVE_STATUSES

    @property
    def rejected(self) -> bool:
        return self.status is OrderStatus.REJECTED


@dataclass(slots=True, frozen=True)
class Balances:
    """Account balances snapshot, in the shape every implementation can fill in."""

    cash: float
    equity: float
    buying_power: float
    realised_pnl_today: float = 0.0
    realised_pnl_week: float = 0.0
    kill_switch_engaged: bool = False


# --------------------------------------------------------------------------
# Guard functions -- the seam the ADR requires to live outside every adapter
# --------------------------------------------------------------------------


def guard_new_order(config: AppConfig, *, is_paper: bool) -> None:
    """Refuse to submit a new order unless authorised and not halted.

    Two independent checks, in order:

    1. Live-trading authorisation (skipped entirely for the paper broker --
       it never transmits anything, so there is nothing to authorise).
    2. The kill switch. Checked for *every* broker, paper included, because a
       halted account must refuse new paper entries too -- ``PaperBroker``
       already enforces this internally via
       ``PaperPortfolio.portfolio_state().kill_switch_engaged`` and
       ``check_new_position``; this is the belt to that suspenders, so the
       refusal holds even if a future adapter's internal check has a bug.

    Only gates *new* positions -- cancelling or modifying an existing order is
    not blocked by the kill switch, see ``guard_live_side_effect``.
    """
    _require_live_authorisation(config, is_paper=is_paper)
    if config.risk.kill_switch_engaged or config.trading.kill_switch_engaged:
        raise TradingHaltedError(
            "kill switch is engaged: no new positions are permitted "
            "(config.risk.kill_switch_engaged or config.trading.kill_switch_engaged)"
        )


def guard_live_side_effect(config: AppConfig, *, is_paper: bool) -> None:
    """Refuse any order-affecting call against a non-paper broker unless authorised.

    Applies to cancel and modify. Deliberately *not* gated on the kill switch:
    an account under an emergency halt must still be able to cancel a stale
    order or tighten a stop to reduce risk -- ``PaperBroker.cancel_all``'s own
    docstring makes the same distinction ("block new entries", never
    "prevent managing what is already open").
    """
    _require_live_authorisation(config, is_paper=is_paper)


def _require_live_authorisation(config: AppConfig, *, is_paper: bool) -> None:
    """Shared check: only a paper broker may act without explicit live opt-in."""
    if is_paper:
        return
    if config.trading.mode != "live":
        raise TradingHaltedError(
            f"trading.mode is {config.trading.mode!r}, not 'live': a non-paper broker "
            "adapter may not transmit, cancel or modify orders outside live mode"
        )
    if not config.trading.live_trading_authorised:
        raise TradingHaltedError(
            "live trading is not authorised: config.trading.live_trading_authorised is False"
        )


# --------------------------------------------------------------------------
# The ABC
# --------------------------------------------------------------------------


class BrokerProvider(ABC):
    """Minimal execution contract every broker adapter implements.

    ``submit_order``/``cancel_order``/``modify_order`` are concrete template
    methods: they run the guard, then delegate to the abstract
    ``_submit_order``/``_cancel_order``/``_modify_order`` an implementation
    supplies. Implementations must set ``self.config: AppConfig`` in their own
    ``__init__`` -- this ABC does not define one, so it does not constrain how
    an adapter wires its dependencies (paper's constructor takes a ``Database``
    a live adapter will not need).
    """

    name: str = "unknown"
    #: Set by each implementation's own ``__init__``; declared here only so
    #: the guarded template methods below have something to type-check against.
    config: AppConfig

    # --- identity ------------------------------------------------------

    @property
    @abstractmethod
    def is_paper(self) -> bool:
        """Whether this adapter simulates fills rather than transmitting them.

        Consulted only at the guard/executor seam -- never inside strategy or
        signal code, which must not know or care which broker is behind it.
        """

    @property
    @abstractmethod
    def is_backtesting(self) -> bool:
        """Whether this adapter is replaying history rather than trading live/paper."""

    # --- guarded order-affecting surface --------------------------------

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        """Submit ``request``. Guarded: see ``guard_new_order``."""
        guard_new_order(self.config, is_paper=self.is_paper)
        return self._submit_order(request)

    def cancel_order(self, order_id: str) -> BrokerOrder:
        """Cancel a live/working order. Guarded: see ``guard_live_side_effect``."""
        guard_live_side_effect(self.config, is_paper=self.is_paper)
        return self._cancel_order(order_id)

    def modify_order(
        self,
        order_id: str,
        *,
        stop_loss: float | None = None,
        targets: list[float] | None = None,
    ) -> BrokerOrder:
        """Adjust an open order's stop and/or targets. Guarded: see ``guard_live_side_effect``."""
        guard_live_side_effect(self.config, is_paper=self.is_paper)
        return self._modify_order(order_id, stop_loss=stop_loss, targets=targets)

    @abstractmethod
    def _submit_order(self, request: OrderRequest) -> BrokerOrder: ...

    @abstractmethod
    def _cancel_order(self, order_id: str) -> BrokerOrder: ...

    @abstractmethod
    def _modify_order(
        self, order_id: str, *, stop_loss: float | None, targets: list[float] | None
    ) -> BrokerOrder: ...

    # --- read-only surface (unguarded: querying state has no live effect) --

    @abstractmethod
    def get_balances(self) -> Balances: ...

    @abstractmethod
    def get_positions(self) -> list[Trade]: ...

    @abstractmethod
    def get_position(self, symbol: str) -> Trade | None: ...

    @abstractmethod
    def get_order(self, order_id: str) -> BrokerOrder | None: ...

    @abstractmethod
    def get_open_orders(self) -> list[BrokerOrder]: ...
