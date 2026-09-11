"""Core value types shared by the simulator, the datasets and the loop.

Everything here is immutable and dataset-agnostic. See
`docs/design/simulator-interface.md` for the contract these types encode; where a
comment below says "the reason", the spec has the longer version.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

# Nanoseconds since the Unix epoch, UTC. Integer rather than a datetime so bar
# boundary arithmetic is exact -- floating seconds accumulate error over a
# multi-year walk and put ticks on the wrong side of a boundary.
Ts = int

NS_PER_SECOND = 1_000_000_000


class LookaheadError(RuntimeError):
    """Raised when something reaches at or beyond the current instant.

    Not an IndexError: reading past the cursor is a research bug, not a bounds
    slip, and it must not be caught by an incidental `except IndexError`.
    """


class HoldoutLocked(RuntimeError):
    """Raised when the holdout split is requested without an explicit unlock."""


class Split(enum.StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class Side(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(enum.StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(enum.StrEnum):
    IOC = "IOC"      # fill what you can against this tick, cancel the rest
    FOK = "FOK"      # fill in full against this tick or cancel
    GTC = "GTC"      # rests until filled or cancelled
    GTD = "GTD"      # rests until `expire_ts`
    DAY = "DAY"      # rests until the session closes


class OrderStatus(enum.StrEnum):
    PENDING = "pending"      # submitted, not yet eligible (see T+1 rule)
    WORKING = "working"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class LifecycleKind(enum.StrEnum):
    ORDER_ACCEPTED = "order_accepted"
    ORDER_REJECTED = "order_rejected"
    CANCEL_CONFIRMED = "cancel_confirmed"
    GTD_EXPIRED = "gtd_expired"
    DAY_EXPIRED = "day_expired"
    OCO_CANCELLED = "oco_cancelled"
    SESSION_OPEN = "session_open"
    SESSION_CLOSE = "session_close"


@dataclass(frozen=True, slots=True)
class Tick:
    ts: Ts
    price: float
    size: int
    session: int          # session index; bars anchor to a session's first tick
    seq: int = 0          # position in the walk, assigned by the engine
    # Aggressor. `None` where the feed does not say, which a strategy must handle
    # rather than assume -- the alternative is silently treating "unknown" as
    # "buy" in every order-flow feature ever written against it.
    side: "Side | None" = None


@dataclass(frozen=True, slots=True)
class DataCapabilities:
    """What a dataset can support. A fill model declares what it requires, and
    the mismatch is caught at wiring time rather than mid-run."""

    has_trades: bool = True
    has_quotes: bool = False
    has_book: bool = False

    def satisfies(self, required: "DataCapabilities") -> bool:
        return (
            (self.has_trades or not required.has_trades)
            and (self.has_quotes or not required.has_quotes)
            and (self.has_book or not required.has_book)
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "has_trades": self.has_trades,
            "has_quotes": self.has_quotes,
            "has_book": self.has_book,
        }


SUBSCRIPTION_KINDS = ("time", "tick")


@dataclass(frozen=True, slots=True)
class Subscription:
    """A request for data. `kind` is what lets a new aggregation slot in.

    `interval_ns` is optional because not every kind has one: a tick
    subscription delivers every trade event and has nothing to bucket by. It was
    a required positive integer until Task 7, which is the part that had to
    change -- the implementation could have been added behind the old type only
    by inventing a meaningless interval for ticks.

    Volume, dollar and imbalance bars are the next kinds. They fit the same tag
    without touching the event loop, which is the point of the tag.
    """

    interval_ns: int | None = None
    kind: str = "time"
    emit_empty: bool = False
    sub_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in SUBSCRIPTION_KINDS:
            raise NotImplementedError(
                f"unknown subscription kind {self.kind!r}; have {SUBSCRIPTION_KINDS}"
            )
        if self.kind == "time":
            if self.interval_ns is None or self.interval_ns <= 0:
                raise ValueError("a time subscription needs a positive interval_ns")
        else:
            if self.interval_ns is not None:
                raise ValueError(f"a {self.kind} subscription has no interval")
            if self.emit_empty:
                raise ValueError(f"emit_empty means nothing for a {self.kind} subscription")
        if not self.sub_id:
            suffix = "" if self.interval_ns is None else f":{self.interval_ns}"
            object.__setattr__(self, "sub_id", f"{self.kind}{suffix}")

    @property
    def is_bars(self) -> bool:
        return self.kind == "time"

    def as_dict(self) -> dict[str, Any]:
        return {
            "sub_id": self.sub_id,
            "kind": self.kind,
            "interval_ns": self.interval_ns,
            "emit_empty": self.emit_empty,
        }


@dataclass(frozen=True, slots=True)
class Bar:
    """Stamped with its open, delivered on its close.

    Both timestamps are carried because stamping by open while delivering at
    close is exactly where a downstream join silently grabs future data. Nothing
    downstream should have to infer which one it is holding.
    """

    sub_id: str
    open_ts: Ts
    close_ts: Ts
    open: float
    high: float
    low: float
    close: float
    volume: int
    tick_count: int
    session: int

    @property
    def is_empty(self) -> bool:
        return self.tick_count == 0


@dataclass(frozen=True, slots=True)
class BarEvent:
    """Every bar that closed at this timestamp, in one callback.

    One callback per timestamp, never one per timeframe: if a 5m and a 1m bar
    both close at 09:35 and arrive separately, the ordering between them becomes
    a hidden variable that affects results and is recorded nowhere.
    """

    ts: Ts
    seq: int
    bars: tuple[Bar, ...]

    def by_sub(self, sub_id: str) -> Bar | None:
        for b in self.bars:
            if b.sub_id == sub_id:
                return b
        return None


@dataclass(frozen=True, slots=True)
class TickEvent:
    """Every trade event, for strategies that build their own representation.

    The premise of the split is that the immutable side provides raw data and the
    agent decides what to make of it. Time bars alone left the aggregation choice
    being made for the agent: volume bars, dollar bars, tick imbalance,
    cumulative delta and every order-flow feature need the events themselves.

    Expensive by design. A few hundred thousand trades a session is tens of
    millions of callbacks across a split, and the wall-clock budget is what
    disciplines that -- a strategy wanting tick granularity pays for it in
    trials, which is an honest trade rather than a hidden one.
    """

    ts: Ts
    seq: int
    price: float
    size: int
    side: Side | None      # aggressor; None where the feed does not say
    session: int


@dataclass(frozen=True, slots=True)
class Fill:
    price: float
    size: int


@dataclass(frozen=True, slots=True)
class FillEvent:
    """Present-instant state only -- never a partially formed bar.

    A strategy that wants finer intra-bar state subscribes to a finer timeframe.
    One rule instead of two.
    """

    ts: Ts
    seq: int
    order_id: int
    side: Side
    price: float
    size: int
    position: int
    last_trade_price: float
    working_order_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    ts: Ts
    seq: int
    kind: LifecycleKind
    order_id: int | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BracketSpec:
    """Stop and target armed atomically by the simulator when the entry fills.

    Strategy-managed exits are where the historical fade artefact lived: an entry
    filled at 12:30:05 sits unprotected until the next bar callback at 12:35.

    Levels come either as absolute prices or as distances from the fill. Absolute
    is what a strategy usually means -- "stop below that swing low" is a level,
    not a distance -- but a distance is the only form available before a market
    order's fill price is known. Both are supported; mixing them is rejected at
    the context.
    """

    stop_offset: float | None = None
    target_offset: float | None = None
    stop_price: float | None = None
    target_price: float | None = None

    def __post_init__(self) -> None:
        for name in ("stop_offset", "target_offset"):
            v = getattr(self, name)
            if v is not None and v <= 0:
                raise ValueError(f"{name} is a positive distance from the fill price")
        if (self.stop_offset is not None and self.stop_price is not None) or (
            self.target_offset is not None and self.target_price is not None
        ):
            raise ValueError("give a bracket level as a price or as an offset, not both")

    @property
    def is_empty(self) -> bool:
        return not any(
            (self.stop_offset, self.target_offset, self.stop_price, self.target_price)
        )


@dataclass
class Order:
    order_id: int
    side: Side
    size: int
    order_type: OrderType
    tif: TimeInForce
    limit_price: float | None = None
    stop_price: float | None = None
    expire_ts: Ts | None = None
    bracket: BracketSpec | None = None
    parent_id: int | None = None
    oco_group: int | None = None
    reduce_only: bool = False
    # The T+1 rule lives here: an order submitted while handling the callback at
    # sequence S is not resolvable until a strictly later tick.
    eligible_from_seq: int = 0
    status: OrderStatus = OrderStatus.PENDING
    filled_size: int = 0
    submitted_ts: Ts = 0
    stop_armed: bool = False   # stop-limit: becomes a limit once the stop trades

    @property
    def remaining(self) -> int:
        return self.size - self.filled_size

    @property
    def is_working(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.WORKING)


@dataclass(frozen=True, slots=True)
class OrderView:
    """What a strategy sees of an order. A copy, never the engine's object.

    `ctx.working_orders` used to hand back the live `Order` instances. Assigning
    to one worked: `order.size = 99` took effect immediately, skipping the intent
    queue, the action cap and the T+1 rule, and reaching the run record nowhere.
    The trial that ran was then not the trial that was logged, which is the one
    thing the ledger exists to prevent.

    Every field is a primitive, an enum or `None`. `frozen=True` protects only
    the top level -- a frozen dataclass holding a list is still mutable through
    that list -- so there are deliberately no containers here.

    `tag` is carried because a strategy could not otherwise recognise its own
    orders: the tag reached the blotter but lived in an engine-private map, so
    "which of these is my protective stop?" had to be answered by inspecting
    order types and prices, or by tracking ids by hand across lifecycle events.
    """

    id: int
    tag: str
    side: Side
    size: int
    filled: int
    remaining: int
    order_type: OrderType
    limit_price: float | None
    stop_price: float | None
    tif: TimeInForce
    parent_id: int | None
    status: OrderStatus

    @classmethod
    def of(cls, order: "Order", tag: str = "") -> "OrderView":
        return cls(
            id=order.order_id,
            tag=tag,
            side=order.side,
            size=order.size,
            filled=order.filled_size,
            remaining=order.remaining,
            order_type=order.order_type,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            tif=order.tif,
            parent_id=order.parent_id,
            status=order.status,
        )


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Submit:
    side: Side
    size: int
    order_type: OrderType = OrderType.MARKET
    tif: TimeInForce = TimeInForce.DAY
    limit_price: float | None = None
    stop_price: float | None = None
    expire_ts: Ts | None = None
    bracket: BracketSpec | None = None
    reduce_only: bool = False
    tag: str = ""


@dataclass(frozen=True, slots=True)
class Modify:
    order_id: int
    limit_price: float | None = None
    stop_price: float | None = None
    size: int | None = None


@dataclass(frozen=True, slots=True)
class Cancel:
    order_id: int


Action = Submit | Modify | Cancel


# --------------------------------------------------------------------------- #
# blotter
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BlotterRow:
    seq: int
    ts: Ts
    order_id: int
    side: Side
    price: float
    size: int
    position_after: int
    commission: float
    realized_pnl: float
    tag: str = ""

    def as_row(self) -> tuple:
        """Stable tuple used for the determinism digest. Floats are rounded to a
        fixed number of places so a byte comparison is meaningful."""
        return (
            self.seq,
            self.ts,
            self.order_id,
            str(self.side),
            round(self.price, 10),
            self.size,
            self.position_after,
            round(self.commission, 10),
            round(self.realized_pnl, 10),
            self.tag,
        )


@dataclass
class Blotter:
    rows: list[BlotterRow] = field(default_factory=list)
    final_position: int = 0
    final_price: float = 0.0

    def add(self, row: BlotterRow) -> None:
        self.rows.append(row)

    @property
    def realized_pnl(self) -> float:
        return sum(r.realized_pnl for r in self.rows)

    @property
    def commission(self) -> float:
        return sum(r.commission for r in self.rows)

    def digest(self) -> str:
        import hashlib
        import json

        payload = json.dumps([r.as_row() for r in self.rows], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ScoreTuple:
    """What the loop accepts or rejects on.

    `primary` is the single scalar -- Sortino on per-session returns. Sharpe,
    skew and kurtosis ride along on the same series because DSR and PBO are
    defined for Sharpe, and a committee with nothing to deflate is awkward to
    retrofit. `rejected` carries the observation floor: below it the ratio is not
    a measurement, and `primary` is the reject sentinel rather than a large
    number computed from a handful of sessions.
    """

    primary: float
    sharpe: float
    skew: float
    kurtosis: float
    n_sessions: int
    net_pnl: float
    trade_count: int
    rejected: bool = False
    reject_reason: str = ""
    metric: str = "sortino_per_session"

    @property
    def significance(self) -> float:
        """The t-like quantity the accept rule thresholds: ratio x sqrt(N).

        Exact for Sharpe, an approximation for Sortino, and stated as such -- it
        is here so the Task 3 threshold of 3.0 could be carried over unchanged
        rather than tuned to make a curve look better.
        """
        if self.rejected or self.n_sessions <= 0 or self.primary == float("-inf"):
            return float("-inf")
        if self.primary == float("inf"):
            return float("inf")
        return self.primary * math.sqrt(self.n_sessions)

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "primary": self.primary,
            "significance": self.significance,
            "sharpe": self.sharpe,
            "skew": self.skew,
            "kurtosis": self.kurtosis,
            "n_sessions": self.n_sessions,
            "net_pnl": self.net_pnl,
            "trade_count": self.trade_count,
            "rejected": self.rejected,
            "reject_reason": self.reject_reason,
        }


class Strategy(Protocol):
    """The shape of `src/strategies/current.py`. See `docs/agent/strategy-api.md`.

    Every callback takes the context and returns nothing. Actions are *queued*
    through the context -- `ctx.buy(...)` records an intent and the engine applies
    it after the callback returns -- so there is no return value to collect, and
    the event is reached through the context too (`ctx.closed`, `ctx.fill`,
    `ctx.tick`, `ctx.event`) rather than passed alongside it.

    This declaration carried the pre-Task-5 shape until Task 12 -- `(ev, ctx)`
    arguments and a returned `Sequence[Action]` -- which nothing imported and so
    nothing caught. It was the only statement of the contract in the code, and it
    described an interface that had not existed for four tasks. The engine
    refuses the old shape at run time (`setup()` returning subscriptions raises),
    but a stale type is still the wrong thing for a reader to find.

    `on_tick` is not here: it is required only of a strategy that subscribes to
    ticks, and `TickStrategy` below is that shape. Splitting them is what lets
    the engine state the requirement precisely instead of checking `hasattr` for
    one callback and reaching for the others mid-run.

    This declaration is **load-bearing**: `engine.validate_strategy()` checks the
    loaded strategy against it before the walk starts, and a test asserts the
    methods declared here are exactly the ones the engine calls. It carried the
    pre-Task-5 shape until Task 12 -- `(ev, ctx)` arguments returning a
    `Sequence[Action]` -- which nothing imported and so nothing caught, for four
    tasks.
    """

    def setup(self, ctx: Any) -> None: ...
    def on_bars(self, ctx: Any) -> None: ...
    def on_fill(self, ctx: Any) -> None: ...
    def on_lifecycle(self, ctx: Any) -> None: ...


class TickStrategy(Strategy, Protocol):
    """A strategy that declared `ctx.subscribe("tick")`, and so must handle them."""

    def on_tick(self, ctx: Any) -> None: ...
