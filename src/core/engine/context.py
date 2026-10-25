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

# Imported for the annotation only. `scripts/gen_remote_surface.py` reads
# `ctx.options()`'s return annotation as TEXT to pick an encoder, so the name
# has to appear -- but the engine importing a dataset module at runtime would
# invert the layering for the sake of a type name.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.data.options import OptionQuote

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Sequence

from ..types import (
    DEFAULT_ASSET,
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
    OrderView,
    Side,
    Submit,
    Subscription,
    Tick,
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


@dataclass(frozen=True, slots=True)
class EnginePort:
    """The operations a context may perform, bound one by one.

    **A context does not hold the engine.** It used to, as `self._engine`, and
    `ctx._dataset` sat beside it -- so a strategy could read the split and the
    seed, call `unlock_holdout`, pull a holdout tick and mutate engine state,
    all through two underscore-prefixed attributes. `tests/test_boundary.py`
    walked only public names and so never saw it: the test encoded the same
    assumption the code did, which is how a guard passes for seven tasks while
    guarding nothing.

    Hiding the attribute would not have helped. The fix is not to hold the
    reference: every entry below is a callable bound to the narrowest object that
    can answer it, so there is no engine at the end of any attribute path.

    **This is defence in depth, not a boundary.** Python has no in-process
    isolation: a determined caller can walk `__closure__` and `__self__`, and
    what stops it there is that those cells hold small state records rather than
    the engine. The boundary is the module-hash check on the scoring code and,
    eventually, a separate process. Said plainly here because the previous
    version of this file implied otherwise by omission.
    """

    position: Any
    last_price: Any
    average_entry_price: Any
    realized_pnl: Any
    session_bounds: Any
    working_ids: Any
    order_views: Any
    order_state: Any
    resolve_sub: Any
    history: Any
    tick_history: Any
    position_in: Any = None
    average_entry_price_in: Any = None
    realized_pnl_in: Any = None
    assets_held: Any = None
    # None when the dataset has no options channel, so `ctx.options()` can say
    # so by name instead of raising an AttributeError from inside the engine.
    options_snapshot: Any = None


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
        "_port",
        "_queue",
        "_active",
        "_ts",
        "_seq",
        "_session",
        "_fills",
        "_actionable",
        "_closed",
    )

    def __init__(self, port: EnginePort, queue: IntentQueue, *, ts: Ts, seq: int,
                 session: int, fills: Sequence[FillEvent], actionable: bool,
                 closed: Sequence[Bar] = ()):
        self._port = port
        self._queue = queue
        self._active = True
        self._ts = ts
        self._seq = seq
        self._session = session
        self._fills = tuple(fills)
        self._actionable = actionable
        # Empty outside on_bars: nothing closed at this instant. Carried on the
        # base so `ctx.bars` means the same thing in every callback rather than
        # existing in one of them.
        self._closed = tuple(closed)

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

    @property
    def session_start(self) -> Ts:
        """When this session opened, in nanoseconds UTC."""
        self._check()
        return self._port.session_bounds()[0]

    @property
    def session_end(self) -> Ts:
        """When this session closes, in nanoseconds UTC.

        **Not lookahead.** An exchange publishes its calendar in advance, so the
        close is known at the open; it is a fact about the schedule, not about
        the data. Nothing here says anything about what happens between now and
        then.

        It exists because without it a strategy cannot be reliably flat at a
        boundary. Every alternative is a guess at the session's length from what
        it has already seen, and every such guess is wrong on the first session
        shorter than anything before it -- a half day, which arrives without
        warning and is exactly when being caught long overnight matters. The
        calibration apparatus guessed from a bar count, which on real data
        measured activity rather than length and collapsed its trading window to
        a third of the session for most of a 384-session run.

        `ctx.session_end - ctx.ts` is the time left. Deciding what margin to
        leave is yours; knowing the deadline should not have been.
        """
        self._check()
        return self._port.session_bounds()[1]

    # ---- state ------------------------------------------------------------- #
    @property
    def position(self) -> int:
        self._check()
        return self._port.position()

    @property
    def last_price(self) -> float:
        self._check()
        return self._port.last_price()

    def position_in(self, asset: str) -> int:
        """The position in one asset. `ctx.position` is this for the default.

        A companion rather than an argument on the property: `ctx.position` is
        read by 65 call sites and by every one of the 3,447 committed
        strategies, and a property cannot take one anyway. The pattern is the
        same for the three below.
        """
        self._check()
        return self._port.position_in(asset)

    def average_entry_price_in(self, asset: str) -> float:
        self._check()
        return self._port.average_entry_price_in(asset)

    def realized_pnl_in(self, asset: str) -> float:
        self._check()
        return self._port.realized_pnl_in(asset)

    def assets_held(self) -> tuple[str, ...]:
        """Every asset with an open book, default first.

        A strategy holding several legs needs to be able to ask what it is
        carrying without remembering what it opened.
        """
        self._check()
        return self._port.assets_held()

    @property
    def working_orders(self) -> tuple[OrderView, ...]:
        """Read-only copies, in the order the engine holds them.

        Copies rather than the engine's own `Order` objects: assigning to one of
        those worked, and bypassed the queue, the cap, the T+1 rule and the run
        record.
        """
        self._check()
        return self._port.order_views()

    def find_orders(
        self,
        *,
        tag: str | None = None,
        order_type: str | OrderType | None = None,
        side: str | Side | None = None,
        parent_id: int | None = None,
    ) -> tuple[OrderView, ...]:
        """Working orders matching every filter given. Empty tuple if none do.

        `ctx.find_orders(tag="protective-stop")` is the intended way to get hold
        of an order you placed earlier, and `parent_id=` is how you reach the two
        legs of a bracket the engine armed for you. Tracking ids by hand across
        lifecycle events also works, and is exactly where a strategy holding an
        id through a DAY expiry crashes.
        """
        self._check()
        want_type = OrderType(order_type) if isinstance(order_type, str) else order_type
        want_side = Side(side) if isinstance(side, str) else side
        return tuple(
            o for o in self._port.order_views()
            if (tag is None or o.tag == tag)
            and (want_type is None or o.order_type is want_type)
            and (want_side is None or o.side is want_side)
            and (parent_id is None or o.parent_id == parent_id)
        )

    @property
    def working_order_ids(self) -> tuple[int, ...]:
        self._check()
        return self._port.working_ids()

    @property
    def fills(self) -> tuple[FillEvent, ...]:
        """Fills since the previous callback."""
        self._check()
        return self._fills

    @property
    def realized_pnl(self) -> float:
        self._check()
        return self._port.realized_pnl()

    @property
    def unrealized_pnl(self) -> float:
        self._check()
        p = self._port
        if not p.position():
            return 0.0
        return (p.last_price() - p.average_entry_price()) * p.position()

    @property
    def equity(self) -> float:
        self._check()
        return self.realized_pnl + self.unrealized_pnl

    @property
    def average_entry_price(self) -> float:
        self._check()
        return self._port.average_entry_price()

    def ticks(self) -> HistoryView[TickEvent]:
        """Every trade event seen so far, oldest first.

        Only populated when `setup()` subscribed to ticks -- the engine does not
        pay to retain them otherwise. Reaching at or beyond the current instant
        raises, exactly as for bars.
        """
        self._check()
        return self._port.tick_history()

    def options(self) -> "tuple[OptionQuote, ...]":
        """The options surface as of this instant, as a tuple of quotes.

        **Why the whole cross-section and not a stream.** An arbitrage is a
        statement about several quotes at once -- parity needs a call and a put,
        a butterfly needs three strikes, a calendar needs two expiries -- so a
        feed that delivered them one at a time would make the task reassembly
        rather than detection. `ctx.history(...)` is the right shape for a
        scalar series and the wrong shape for a surface.

        **There is no look-ahead to refuse here, and that is a property of the
        construction rather than a check.** The surface is a deterministic
        function of the underlying price, and the price it is built from is the
        last trade at or before this instant -- the same number `ctx.last_price`
        returns. There is no future quote in the dataset to leak, so unlike
        `history()` this method needs no `LookaheadError`: a strategy cannot
        ask for a surface the engine has not reached, because the surface is
        computed from what the engine has already seen.

        Raises `ContextError` when the dataset has no options channel, naming
        the capability, rather than returning an empty tuple that would read as
        "no quotes right now".
        """
        self._check()
        if self._port.options_snapshot is None:
            raise ContextError(
                "this dataset has no options channel: its capabilities report "
                "has_options=False. A strategy that needs the surface should "
                "run on a dataset that provides one -- the mismatch is meant to "
                "be caught at wiring time, not worked around here.")
        return self._port.options_snapshot(self._ts, self._session)

    def sub_id(self, sub: str | Subscription) -> str:
        """The canonical id of a subscription you declared.

        `ctx.sub_id("5m")` -> `"time:300000000000"`. Ids are canonical, not the
        text you typed, and an unknown one raises rather than quietly not
        matching -- so this is here for the rare code that needs the canonical
        form, not as something every strategy has to remember to call.
        """
        self._check()
        return self._port.resolve_sub(sub)

    def history(self, sub: str | Subscription) -> HistoryView[Bar]:
        """Closed bars for a subscription, oldest first.

        Takes whatever you subscribed with (`"5m"`), the canonical id, an
        equivalent spelling (`"300s"`), or the `Subscription` itself. An id you
        never subscribed to raises `ContextError`.

        Reaching at or beyond the current instant raises `LookaheadError` -- the
        newest readable element is the bar that just closed.
        """
        self._check()
        return self._port.history(self._port.resolve_sub(sub))

    @property
    def closed(self) -> tuple[Bar, ...]:
        """Every bar that closed at this instant, in one callback.

        Empty in every callback but `on_bars`.
        """
        self._check()
        return self._closed

    def closed_ids(self) -> tuple[str, ...]:
        self._check()
        return tuple(b.sub_id for b in self._closed)

    @property
    def bars(self) -> "_ClosedBars":
        """The bars that closed **in this callback**, addressed by subscription.

        `if "5m" in ctx.bars:` is the question a multi-timeframe strategy
        actually has, and it is now the expression that answers it. It used to
        mean "the most recent closed bar", which made the obvious membership test
        true whenever any 5m bar had ever closed -- and made the obvious
        `"5m" in ctx.closed_ids()` silently false, because ids are canonical.
        A strategy that never trades and says nothing about why is the worst
        failure mode there is.

        For the most recent closed bar, whenever it closed, use
        `ctx.history("5m")[-1]`.
        """
        self._check()
        return _ClosedBars(self)

    # ---- writes ------------------------------------------------------------ #
    def buy(self, size: int = 1, **kw) -> int | None:
        """Submit a buy. Returns the order id, or None if the action cap dropped it.

        `asset=` names what to trade and defaults to the one instrument a
        single-asset run has, so every strategy written before assets existed
        means exactly what it meant. `ctx.buy(1)` and
        `ctx.buy(1, asset="SPX_C_7850_DEC26")` are different books.
        """
        return self._order(Side.BUY, size, **kw)

    def sell(self, size: int = 1, **kw) -> int | None:
        return self._order(Side.SELL, size, **kw)

    def close(self, **kw) -> int | None:
        """Flatten the current position with a market order."""
        self._check()
        pos = self._port.position()
        if pos == 0:
            raise ContextError("close() with no position open")
        side = Side.SELL if pos > 0 else Side.BUY
        # **The caller's tag wins.** This read `tag="close", **kw`, so a
        # strategy that passed a tag -- which `docs/agent/strategy-api.md`
        # documents as legal, "free text; lands in the blotter row", and which
        # `close(**kw)` advertises by accepting anything -- got
        # `_order() got multiple values for keyword argument 'tag'`. Three of
        # nq-s07-02's crashes were that: the model did a reasonable thing and the
        # engine raised a TypeError at it. "close" stays the default.
        return self._order(side, abs(pos), reduce_only=True,
                           **{"tag": "close", **kw})

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
        asset: str = DEFAULT_ASSET,
    ) -> int | None:
        self._check()
        if not isinstance(asset, str) or not asset:
            raise ContextError(
                f"asset must be a non-empty name, got {asset!r}. An order has "
                "to say what it is for: there is no 'whichever book is open'.")
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
            ref = limit if limit is not None else self._port.last_price()
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
                asset=asset,
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
        state = self._port.order_state(order_id)
        if state is None:
            raise ContextError(f"no order {order_id}")
        working, status = state
        if not working:
            raise ContextError(
                f"cannot modify order {order_id}: it is {status}"
            )
        if limit is None and trigger is None and size is None:
            raise ContextError("modify() with nothing to change")
        if size is not None and (not isinstance(size, int) or size <= 0):
            raise ContextError(f"size must be a positive whole number, got {size!r}")
        self._queue.add(Modify(order_id=order_id, limit_price=limit,
                               stop_price=trigger, size=size))

    def cancel(self, order_id: int) -> None:
        self._check()
        state = self._port.order_state(order_id)
        if state is None:
            raise ContextError(f"no order {order_id}")
        working, status = state
        if not working:
            raise ContextError(
                f"cannot cancel order {order_id}: it is already {status}"
            )
        self._queue.add(Cancel(order_id=order_id))

    def cancel_all(self) -> None:
        self._check()
        for oid in self._port.working_ids():
            self._queue.add(Cancel(order_id=oid))


class _ClosedBars:
    """`ctx.bars` -- the bars that closed in this callback, by subscription.

    A read-only mapping. Membership and lookup both resolve the id through the
    engine, so an id that was never subscribed raises instead of reading as
    absent: a never-subscribed id is a bug, and a bug that presents as "no bar"
    costs trials before anyone works out why nothing traded.
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: BaseContext):
        self._ctx = ctx

    def __getitem__(self, sub: str | Subscription) -> Bar:
        sid = self._ctx.sub_id(sub)
        for bar in self._ctx.closed:
            if bar.sub_id == sid:
                return bar
        raise ContextError(
            f"{sid!r} did not close in this callback; test with "
            f"`if {sub!r} in ctx.bars:` first, or use ctx.history(...) for the "
            "most recent close whenever it happened"
        )

    def __contains__(self, sub: str | Subscription) -> bool:
        sid = self._ctx.sub_id(sub)
        return any(bar.sub_id == sid for bar in self._ctx.closed)

    def get(self, sub: "str | Subscription", default=None):
        """`self[sub]` if that bar closed in this callback, else `default`.

        **Added 2026-10-05 because its absence crashed 13 trials.** This object
        answers "did my bar close just now", and the natural Python for that is
        `ctx.bars.get("5m")` -- it supports `__getitem__`, `__contains__`,
        `__len__` and `__iter__`, so a model reasonably reads it as a mapping and
        reaches for the one mapping method it did not have. Every one of those
        proposals was correct about what it wanted and died on the spelling.

        Not a loosening of anything: it returns exactly what `sub in self` then
        `self[sub]` returns, and no strategy could previously have called it.
        """
        try:
            return self[sub]
        except Exception:
            return default

    def __len__(self) -> int:
        return len(self._ctx.closed)

    def __iter__(self):
        return iter(self._ctx.closed_ids())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ClosedBars({list(self._ctx.closed_ids())})"


def canonical_sub_id(sub: str | Subscription) -> str:
    """Spelling to canonical id. Does **not** check the id was subscribed.

    Engine-side only: every strategy-facing entry point goes through
    `Engine.resolve_sub`, which canonicalises here and then refuses anything that
    was not declared.
    """
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

    def __init__(self, port, queue, *, tick: TickEvent, **kw):
        super().__init__(port, queue, **kw)
        self._tick = tick

    @property
    def tick(self) -> TickEvent:
        self._check()
        return self._tick


class BarContext(BaseContext):
    """Delivered when one or more subscribed bars close at this timestamp.

    `ctx.closed`, `ctx.closed_ids()` and `ctx.bars` are on the base context and
    are empty everywhere else, so the same expression reads correctly in every
    callback rather than existing in one of them.
    """

    __slots__ = ("_event",)

    def __init__(self, port, queue, *, event: BarEvent, **kw):
        super().__init__(port, queue, closed=event.bars, **kw)
        self._event = event


class FillContext(BaseContext):
    """Delivered on a fill. Present-instant state only, never a partial bar."""

    __slots__ = ("_fill",)

    def __init__(self, port, queue, *, fill: FillEvent, **kw):
        super().__init__(port, queue, **kw)
        self._fill = fill

    @property
    def fill(self) -> FillEvent:
        self._check()
        return self._fill


class LifecycleContext(BaseContext):
    __slots__ = ("_event",)

    def __init__(self, port, queue, *, event: LifecycleEvent, **kw):
        super().__init__(port, queue, **kw)
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

    **Bulk, but not materialised.** `train_ticks` is a factory returning a fresh
    iterator, not a list. The engine used to build the list eagerly and this
    class copied it again, which cost nothing on the synthetic generator's 68,000
    training ticks and was fatal on the first real run: the NQ training split is
    384 sessions of roughly 321,000 trades, so a run that never looked at a
    training tick still tried to hold about 247 million of them twice, on the
    order of 22 GiB, before `setup()` was called. A strategy that wants the ticks
    still gets them and still pays for them; one that does not pays nothing.
    """

    __slots__ = ("_name", "_session_bounds", "_train_ticks", "_subs", "_active")

    def __init__(self, dataset_name: str, session_bounds, train_ticks):
        # The dataset's *name* and one bound accessor, not the dataset. It was
        # `self._dataset`, which put `unlock_holdout` and the whole tick stream
        # one underscore from strategy code -- a validation strategy could open
        # the holdout and read a tick from it, and the adversarial review did
        # exactly that.
        self._name = dataset_name
        self._session_bounds = session_bounds
        # A callable is the contract. An iterable is accepted so a caller with a
        # list in hand -- a test, usually -- still works, and is wrapped so it
        # can be walked more than once.
        if callable(train_ticks):
            self._train_ticks = train_ticks
        else:
            materialised = tuple(train_ticks)
            self._train_ticks = lambda: iter(materialised)
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
        return self._name

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

    def subscriptions(self) -> tuple[Subscription, ...]:
        return tuple(self._subs)

    def ticks(self) -> tuple[Tick, ...]:
        """Every train tick, in order.

        Materialises the whole training split, which on a real dataset is
        hundreds of millions of events -- 384 NQ sessions at roughly 321,000
        trades each. Nothing stops you; it is your memory and your wall-clock
        budget. `train_bars()` streams the same data through the aggregator
        instead, and is what you want unless you are building something the bar
        builder cannot express.
        """
        self._check()
        return tuple(self._train_ticks())

    def train_bars(self, interval: str | int | Subscription) -> tuple[Bar, ...]:
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
        # Streams: `aggregate` iterates once and keeps only the closed bars.
        return tuple(aggregate(self._train_ticks(), sub, self._session_bounds))
