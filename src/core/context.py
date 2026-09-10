"""What a strategy is handed, and the only surface it can act through.

A strategy calls `ctx.buy(...)`; the call is **queued**, the callback returns,
and the engine applies the queue. The facade is immutable code, so nothing is
given up by making it convenient: the recorded intent list is exactly what the
strategy used to return, and T+1 eligibility, the action cap and auditability are
untouched.

It is convenient on purpose. An LLM writes hundreds of these files, and
`ctx.buy(...)` is the idiom in backtrader, zipline, QuantConnect and freqtrade,
with a great deal of it in training data; assembling lists of dataclasses is not.
A method can also reject `size=0`, or a stop on the wrong side of the entry, with
a message naming the rule at the moment of the mistake. Every malformed action
was a wasted trial and a wasted entry in N.

**The boundary is the objective function, not P&L.** A strategy may know how it
is doing -- it needs position P&L for risk logic, it has it in live trading, and
it could reconstruct it from its own fills anyway. It may not know how it is
being judged: nothing here exposes the score, anything the scorer computes, the
split identity, the seed, or any data beyond the current instant.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Sequence

from .types import (
    NS_PER_SECOND,
    Action,
    Bar,
    BarEvent,
    BracketSpec,
    Cancel,
    FillEvent,
    LifecycleEvent,
    Modify,
    Order,
    OrderType,
    Side,
    Submit,
    Subscription,
    TickEvent,
    TimeInForce,
    Ts,
)
from .view import BarHistory, HistoryView


class ContextError(RuntimeError):
    """A strategy asked for something the simulator will not do.

    Raised at the call, naming the rule, rather than accepted and silently
    dropped -- a silent no-op is a trial that looks like a result.
    """


class ContextExpired(ContextError):
    """The context was used outside the callback it belongs to.

    Stashing `ctx` in `setup()` and reading it later would let a strategy read
    state from an instant it is not standing in.
    """


_DURATION = re.compile(r"^(\d+)\s*(ns|us|ms|s|m|h|d)$")
_UNITS = {
    "ns": 1,
    "us": 1_000,
    "ms": 1_000_000,
    "s": NS_PER_SECOND,
    "m": 60 * NS_PER_SECOND,
    "h": 3600 * NS_PER_SECOND,
    "d": 86400 * NS_PER_SECOND,
}


def parse_interval(spec: str | int) -> int:
    """`"5m"` -> nanoseconds. Integers pass through as nanoseconds."""
    if isinstance(spec, int):
        if spec <= 0:
            raise ContextError(f"interval must be positive, got {spec}")
        return spec
    m = _DURATION.match(str(spec).strip().lower())
    if not m:
        raise ContextError(
            f"cannot read {spec!r} as an interval; use e.g. '30s', '5m', '1h', '1d'"
        )
    return int(m.group(1)) * _UNITS[m.group(2)]


class IntentQueue:
    """Holds what a callback asked for until the engine applies it.

    The action cap is checked **here**, when a call is queued, so an order
    dropped past the cap never consumes an order id. When the callback is not
    actionable -- a warmup session, where the bars are real but the orders would
    not be -- the queue is inert: nothing is recorded, no id is issued, and the
    calls are counted separately so they are visible in the run record rather
    than silently vanishing.
    """

    __slots__ = ("actions", "cap", "dropped", "cap_hit", "ignored", "_alloc", "_live")

    def __init__(self, cap: int, allocator, *, live: bool = True):
        self.actions: list[tuple[Action, int | None]] = []
        self.cap = cap
        self.dropped = 0
        self.cap_hit = False
        self.ignored = 0
        self._alloc = allocator
        self._live = live

    def _room(self) -> bool:
        if not self._live:
            self.ignored += 1
            return False
        if len(self.actions) >= self.cap:
            self.cap_hit = True
            self.dropped += 1
            return False
        return True

    def add(self, action: Action) -> None:
        if self._room():
            self.actions.append((action, None))

    def add_order(self, submit: Submit) -> int | None:
        """Queues a submit and returns the id it will be given, or None if the
        cap dropped it or the callback is not actionable."""
        if not self._room():
            return None
        oid = self._alloc()
        self.actions.append((submit, oid))
        return oid


class BaseContext:
    """Reads and writes shared by the three run-time callbacks."""

    __slots__ = (
        "_engine",
        "_queue",
        "_active",
        "_ts",
        "_seq",
        "_session",
        "_fills",
        "_actionable",
    )

    def __init__(self, engine, queue: IntentQueue, *, ts: Ts, seq: int,
                 session: int, fills: Sequence[FillEvent], actionable: bool):
        self._engine = engine
        self._queue = queue
        self._active = True
        self._ts = ts
        self._seq = seq
        self._session = session
        self._fills = tuple(fills)
        self._actionable = actionable

    # ---- lifetime ---------------------------------------------------------- #
    def _expire(self) -> None:
        self._active = False

    def _check(self) -> None:
        if not self._active:
            raise ContextExpired(
                "this context belonged to an earlier callback. Read what you need "
                "while the callback is running, or keep it on `self`."
            )

    # ---- clock ------------------------------------------------------------- #
    @property
    def ts(self) -> Ts:
        """Nanoseconds since the epoch, UTC."""
        self._check()
        return self._ts

    @property
    def now(self) -> dt.datetime:
        self._check()
        return dt.datetime.fromtimestamp(self._ts / 1e9, dt.timezone.utc)

    @property
    def session(self) -> int:
        self._check()
        return self._session

    @property
    def actionable(self) -> bool:
        """False during warmup: the bars are real, the orders would not be."""
        self._check()
        return self._actionable

    # ---- state ------------------------------------------------------------- #
    @property
    def position(self) -> int:
        self._check()
        return self._engine.position

    @property
    def last_price(self) -> float:
        self._check()
        return self._engine.last_price

    @property
    def working_orders(self) -> tuple[Order, ...]:
        self._check()
        return tuple(self._engine._working.values())

    @property
    def working_order_ids(self) -> tuple[int, ...]:
        self._check()
        return self._engine._working_ids()

    @property
    def fills(self) -> tuple[FillEvent, ...]:
        """Fills since the previous callback."""
        self._check()
        return self._fills

    @property
    def realized_pnl(self) -> float:
        self._check()
        return self._engine.blotter.realized_pnl - self._engine.blotter.commission

    @property
    def unrealized_pnl(self) -> float:
        self._check()
        e = self._engine
        if not e.position:
            return 0.0
        return (e.last_price - e._avg_price) * e.position

    @property
    def equity(self) -> float:
        self._check()
        return self.realized_pnl + self.unrealized_pnl

    @property
    def average_entry_price(self) -> float:
        self._check()
        return self._engine._avg_price

    def ticks(self) -> HistoryView[TickEvent]:
        """Every trade event seen so far, oldest first.

        Only populated when `setup()` subscribed to ticks -- the engine does not
        pay to retain them otherwise. Reaching at or beyond the current instant
        raises, exactly as for bars.
        """
        self._check()
        return self._engine.tick_history

    def history(self, sub: str | Subscription) -> HistoryView[Bar]:
        """Closed bars for a subscription, oldest first.

        Reaching at or beyond the current instant raises `LookaheadError` -- the
        newest readable element is the bar that just closed.
        """
        self._check()
        return self._engine.history[_sub_id(sub)]

    @property
    def bars(self) -> "_LatestBars":
        """`ctx.bars["5m"]` -- the most recent closed bar for a subscription."""
        self._check()
        return _LatestBars(self)

    # ---- writes ------------------------------------------------------------ #
    def buy(self, size: int = 1, **kw) -> int | None:
        """Submit a buy. Returns the order id, or None if the action cap dropped it."""
        return self._order(Side.BUY, size, **kw)

    def sell(self, size: int = 1, **kw) -> int | None:
        return self._order(Side.SELL, size, **kw)

    def close(self, **kw) -> int | None:
        """Flatten the current position with a market order."""
        self._check()
        pos = self._engine.position
        if pos == 0:
            raise ContextError("close() with no position open")
        side = Side.SELL if pos > 0 else Side.BUY
        return self._order(side, abs(pos), reduce_only=True, tag="close", **kw)

    def _order(
        self,
        side: Side,
        size: int,
        *,
        type: str | OrderType = OrderType.MARKET,
        limit: float | None = None,
        stop: float | None = None,
        target: float | None = None,
        stop_offset: float | None = None,
        target_offset: float | None = None,
        trigger: float | None = None,
        tif: str | TimeInForce = TimeInForce.DAY,
        expire_ts: Ts | None = None,
        reduce_only: bool = False,
        tag: str = "",
    ) -> int | None:
        self._check()
        otype = OrderType(type) if isinstance(type, str) else type
        tif_ = TimeInForce(tif) if isinstance(tif, str) else tif

        if not isinstance(size, int) or size <= 0:
            raise ContextError(
                f"size must be a positive whole number of contracts, got {size!r}; "
                "use sell() rather than a negative size"
            )
        if otype in (OrderType.LIMIT, OrderType.STOP_LIMIT) and limit is None:
            raise ContextError(f"a {otype} order needs limit=<price>")
        if otype in (OrderType.STOP, OrderType.STOP_LIMIT) and trigger is None:
            raise ContextError(
                f"a {otype} order needs trigger=<price>; `stop=` is the protective "
                "stop of a bracket, which is a different thing"
            )
        if otype is OrderType.MARKET and limit is not None:
            raise ContextError("a market order cannot carry a limit price")

        absolute = stop is not None or target is not None
        relative = stop_offset is not None or target_offset is not None
        if absolute and relative:
            raise ContextError(
                "give bracket levels either as prices (stop=, target=) or as "
                "distances (stop_offset=, target_offset=), not both"
            )

        bracket = None
        if absolute:
            ref = limit if limit is not None else self._engine.last_price
            if stop is not None:
                if side is Side.BUY and stop >= ref:
                    raise ContextError(
                        f"stop {stop} must be below the entry {ref} for a long"
                    )
                if side is Side.SELL and stop <= ref:
                    raise ContextError(
                        f"stop {stop} must be above the entry {ref} for a short"
                    )
            if target is not None:
                if side is Side.BUY and target <= ref:
                    raise ContextError(
                        f"target {target} must be above the entry {ref} for a long"
                    )
                if side is Side.SELL and target >= ref:
                    raise ContextError(
                        f"target {target} must be below the entry {ref} for a short"
                    )
            bracket = BracketSpec(stop_price=stop, target_price=target)
        elif relative:
            for name, v in (("stop_offset", stop_offset), ("target_offset", target_offset)):
                if v is not None and v <= 0:
                    raise ContextError(
                        f"{name} is a positive distance from the fill price, got {v}"
                    )
            bracket = BracketSpec(stop_offset=stop_offset, target_offset=target_offset)

        if tif_ is TimeInForce.GTD and expire_ts is None:
            raise ContextError("a GTD order needs expire_ts=<nanoseconds>")

        return self._queue.add_order(
            Submit(
                side=side,
                size=size,
                order_type=otype,
                tif=tif_,
                limit_price=limit,
                stop_price=trigger,
                expire_ts=expire_ts,
                bracket=bracket,
                reduce_only=reduce_only,
                tag=tag,
            )
        )

    def modify(
        self,
        order_id: int,
        *,
        limit: float | None = None,
        trigger: float | None = None,
        size: int | None = None,
    ) -> None:
        self._check()
        order = self._engine.orders.get(order_id)
        if order is None:
            raise ContextError(f"no order {order_id}")
        if not order.is_working:
            raise ContextError(
                f"cannot modify order {order_id}: it is {order.status}"
            )
        if limit is None and trigger is None and size is None:
            raise ContextError("modify() with nothing to change")
        if size is not None and (not isinstance(size, int) or size <= 0):
            raise ContextError(f"size must be a positive whole number, got {size!r}")
        self._queue.add(Modify(order_id=order_id, limit_price=limit,
                               stop_price=trigger, size=size))

    def cancel(self, order_id: int) -> None:
        self._check()
        order = self._engine.orders.get(order_id)
        if order is None:
            raise ContextError(f"no order {order_id}")
        if not order.is_working:
            raise ContextError(
                f"cannot cancel order {order_id}: it is already {order.status}"
            )
        self._queue.add(Cancel(order_id=order_id))

    def cancel_all(self) -> None:
        self._check()
        for oid in self._engine._working_ids():
            self._queue.add(Cancel(order_id=oid))


class _LatestBars:
    """`ctx.bars["5m"]`. Separate from `ctx.history` so the common case is short."""

    __slots__ = ("_ctx",)

    def __init__(self, ctx: BaseContext):
        self._ctx = ctx

    def __getitem__(self, sub: str | Subscription) -> Bar:
        h = self._ctx.history(sub)
        if not len(h):
            raise ContextError(
                f"no bar has closed yet for {_sub_id(sub)!r}; check len(ctx.history(...))"
            )
        return h[-1]

    def __contains__(self, sub: str | Subscription) -> bool:
        try:
            return len(self._ctx.history(sub)) > 0
        except KeyError:
            return False


def _sub_id(sub: str | Subscription) -> str:
    if isinstance(sub, Subscription):
        return sub.sub_id
    s = str(sub).strip()
    if s.lower() in ("tick", "ticks"):
        return "tick"
    if ":" in s:
        return s
    return f"time:{parse_interval(s)}"


class TickContext(BaseContext):
    """Delivered for every trade event, between the fill callbacks it may have
    caused and the bar closes it may trigger."""

    __slots__ = ("_tick",)

    def __init__(self, engine, queue, *, tick: TickEvent, **kw):
        super().__init__(engine, queue, **kw)
        self._tick = tick

    @property
    def tick(self) -> TickEvent:
        self._check()
        return self._tick


class BarContext(BaseContext):
    """Delivered when one or more subscribed bars close at this timestamp."""

    __slots__ = ("_event",)

    def __init__(self, engine, queue, *, event: BarEvent, **kw):
        super().__init__(engine, queue, **kw)
        self._event = event

    @property
    def closed(self) -> tuple[Bar, ...]:
        """Every bar that closed at this timestamp, in one callback."""
        self._check()
        return self._event.bars

    def closed_ids(self) -> tuple[str, ...]:
        self._check()
        return tuple(b.sub_id for b in self._event.bars)


class FillContext(BaseContext):
    """Delivered on a fill. Present-instant state only, never a partial bar."""

    __slots__ = ("_fill",)

    def __init__(self, engine, queue, *, fill: FillEvent, **kw):
        super().__init__(engine, queue, **kw)
        self._fill = fill

    @property
    def fill(self) -> FillEvent:
        self._check()
        return self._fill


class LifecycleContext(BaseContext):
    __slots__ = ("_event",)

    def __init__(self, engine, queue, *, event: LifecycleEvent, **kw):
        super().__init__(engine, queue, **kw)
        self._event = event

    @property
    def event(self) -> LifecycleEvent:
        self._check()
        return self._event


class SetupContext:
    """What `setup()` sees: the train split in bulk, and the subscription call.

    Bulk is the point -- a strategy fitting parameters needs the whole training
    period at once, and there is no lookahead risk in data that is, by
    construction, entirely before the split it will be scored on.
    """

    __slots__ = ("_dataset", "_train_ticks", "_subs", "_active")

    def __init__(self, dataset, train_ticks):
        self._dataset = dataset
        self._train_ticks = list(train_ticks)
        self._subs: list[Subscription] = []
        self._active = True

    def _expire(self) -> None:
        self._active = False

    def _check(self) -> None:
        if not self._active:
            raise ContextExpired(
                "the setup context is only valid during setup(); subscribe there, "
                "and keep anything you fitted on `self`."
            )

    @property
    def dataset_name(self) -> str:
        self._check()
        return self._dataset.name

    def subscribe(self, interval: str | int, *, emit_empty: bool = False) -> Subscription:
        """Declare a subscription.

        `ctx.subscribe("5m")` for time bars: anchored to the session open,
        stamped with their open timestamp, delivered on close. Empty intervals
        are skipped unless `emit_empty=True`, which carries the previous close
        forward at zero volume.

        `ctx.subscribe("tick")` for every trade event, which is what volume
        bars, dollar bars, tick imbalance and any order-flow feature need. It is
        expensive -- hundreds of thousands of callbacks a session -- and the
        wall-clock budget is what makes that an honest cost rather than a hidden
        one.
        """
        self._check()
        if isinstance(interval, str) and interval.strip().lower() in ("tick", "ticks"):
            sub = Subscription(kind="tick", emit_empty=emit_empty)
        else:
            sub = Subscription(interval_ns=parse_interval(interval), emit_empty=emit_empty)
        for existing in self._subs:
            if existing.sub_id == sub.sub_id:
                if existing.emit_empty != sub.emit_empty:
                    raise ContextError(
                        f"{sub.sub_id} already subscribed with "
                        f"emit_empty={existing.emit_empty}"
                    )
                return existing
        self._subs.append(sub)
        return sub

    def subscriptions(self) -> list[Subscription]:
        return list(self._subs)

    def ticks(self):
        """Every train tick, in order."""
        self._check()
        return tuple(self._train_ticks)

    def train_bars(self, interval: str | int | Subscription) -> list[Bar]:
        """Aggregate the train split at an arbitrary interval, through the same
        code path the live walk uses."""
        self._check()
        from .engine import aggregate

        sub = (
            interval
            if isinstance(interval, Subscription)
            else Subscription(interval_ns=parse_interval(interval))
        )
        if not sub.is_bars:
            raise ContextError(
                "train_bars() aggregates; for raw train events use ctx.ticks()"
            )
        return aggregate(self._train_ticks, sub, self._dataset)
