"""The event loop: the immutable side of the split.

Ordering within a tick, and why it is this order:

1. resolve resting orders against the tick  -> FillEvents
2. deliver fill callbacks                    (actionable)
3. deliver one BarEvent for every bar that closed at this timestamp (actionable)

Fills before bars because a fill is a fact about this tick and a bar is a summary
of ticks that ended before it. One BarEvent rather than one per timeframe,
because if a 5m and a 1m bar both close at 09:35 and arrive separately, the
ordering between them becomes a hidden variable that affects results and is
recorded nowhere.

Everything a strategy submits at tick T becomes eligible at T+1, strictly after.
Without that a strategy fills at the close of the bar it just saw, which is a
clean one-bar lookahead producing beautiful and entirely fake results.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .bars import BarBuilder
from .context import (
    BarContext,
    FillContext,
    IntentQueue,
    LifecycleContext,
    SetupContext,
)
from .dataset import Dataset
from .fills import FillModel, check_capabilities, get_fill_model
from .types import (
    Action,
    Bar,
    BarEvent,
    Blotter,
    BlotterRow,
    BracketSpec,
    Cancel,
    Fill,
    FillEvent,
    LifecycleEvent,
    LifecycleKind,
    Modify,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Split,
    Submit,
    Subscription,
    Tick,
    TimeInForce,
    Ts,
)
from .view import BarHistory

DEFAULT_ACTION_CAP = 64


@dataclass
class RunRecord:
    """One row for the trial ledger. The loop owner appends it."""

    dataset: str
    dataset_digest: str
    split: str
    fill_model: str
    capabilities: dict
    seed: int | None
    git_sha: str
    wall_clock_s: float
    subscriptions: list[dict]
    action_cap: int
    action_cap_hits: int
    actions_dropped: int
    warmup_actions_ignored: int
    ticks: int
    bars_delivered: int
    fills: int
    outcome: str
    error: str | None
    warmup_sessions: int
    end_of_session_positions: dict[str, int]
    holdout_unlock_reason: str | None
    blotter_digest: str
    score: dict | None = None
    diagnostics: dict | None = None

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def git_sha() -> str:
    import subprocess

    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() or "unknown"
    except Exception:  # pragma: no cover - git absent is not a run failure
        return "unknown"


def aggregate(ticks: Iterable[Tick], sub: Subscription, dataset: Any) -> list[Bar]:
    """Bars for a tick stream, session-anchored, using the live builder."""
    builder = BarBuilder(sub)
    out: list[Bar] = []
    session = None
    for t in ticks:
        if t.session != session:
            if session is not None:
                tail = builder.flush_session()
                if tail is not None:
                    out.append(tail)
            session = t.session
            builder.start_session(session, dataset.session_bounds(session)[0])
        out.extend(builder.bars_closed_by(t))
        builder.add(t)
    if session is not None:
        tail = builder.flush_session()
        if tail is not None:
            out.append(tail)
    return out


class Engine:
    def __init__(
        self,
        dataset: Any,
        strategy: Any,
        *,
        split: Split = Split.VALIDATION,
        fill_model: FillModel | str = "unfavorable",
        warmup_sessions: int = 0,
        # Price units are the dataset's own. The synthetic generator works in
        # units where a bracket is ~0.75, so the default commission is scaled to
        # be a small fraction of a target rather than larger than one -- a cost
        # that exceeds the target makes every strategy a loser and hides whether
        # the harness can see an edge at all. A real adapter passes its own.
        commission_per_contract: float = 0.01,
        action_cap: int = DEFAULT_ACTION_CAP,
        seed: int | None = None,
    ):
        self.dataset = dataset
        self.strategy = strategy
        self.split = split
        self.model = (
            get_fill_model(fill_model) if isinstance(fill_model, str) else fill_model
        )
        check_capabilities(self.model, dataset.capabilities())
        self.warmup_sessions = warmup_sessions
        self.commission = commission_per_contract
        self.action_cap = action_cap
        self.seed = seed

        self.blotter = Blotter()
        self.orders: dict[int, Order] = {}
        self._next_order_id = 1
        self._next_oco_group = 1
        self.position = 0
        self._avg_price = 0.0
        self.last_price = 0.0
        self._pending_events: list[LifecycleEvent] = []
        self._order_tags: dict[int, str] = {}

        self.action_cap_hits = 0
        self.actions_dropped = 0
        self.warmup_actions_ignored = 0
        self._fills_since_callback: list = []
        self.n_ticks = 0
        self.n_bars = 0
        self.n_fills = 0
        self.end_of_session_positions: dict[str, int] = {}

    # ---- run --------------------------------------------------------------- #
    def run(self) -> RunRecord:
        started = time.monotonic()
        outcome, error = "completed", None
        try:
            self._run_inner()
        except Exception as exc:  # a crash is a ledger row, not a lost trial
            outcome, error = "crashed", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.record = self._make_record(
                outcome, error, time.monotonic() - started
            )
        return self.record

    def _run_inner(self) -> None:
        warm = set(self.dataset.warmup_sessions(self.split, self.warmup_sessions))

        train = (
            list(self.dataset.ticks(Split.TRAIN))
            if self.split is not Split.TRAIN
            else []
        )
        setup_ctx = SetupContext(self.dataset, train)
        declared = self.strategy.setup(setup_ctx)
        setup_ctx._expire()
        subs = list(setup_ctx.subscriptions())
        if declared:
            # tolerate the pre-Task-5 shape rather than silently ignoring it
            raise TypeError(
                "setup() returns nothing now; declare subscriptions with "
                "ctx.subscribe('5m') instead of returning them"
            )
        if not subs:
            raise ValueError(
                "setup() declared no subscriptions; call ctx.subscribe('5m')"
            )
        # Stable order: interval ascending, then id. The single-callback rule is
        # only meaningful if the bars inside it arrive the same way every run.
        subs.sort(key=lambda s: (s.interval_ns, s.sub_id))
        self.subs = subs
        self.builders = {s.sub_id: BarBuilder(s) for s in subs}
        self.history = BarHistory([s.sub_id for s in subs])

        session = None
        for seq, tick in enumerate(self.dataset.ticks(self.split, warmup=self.warmup_sessions)):
            tick = Tick(tick.ts, tick.price, tick.size, tick.session, seq)
            self.n_ticks += 1

            if tick.session != session:
                if session is not None:
                    self._close_session(session, tick.seq, actionable=session not in warm)
                session = tick.session
                self._open_session(session, tick)

            actionable = tick.session not in warm
            self.last_price = tick.price

            # 1. resolve resting orders against this tick
            fills = self._resolve(tick)

            # 2. fill callbacks
            for ev in fills:
                self._fills_since_callback.append(ev)
                self._dispatch("fill", ev, tick, actionable)

            # 3. bars that closed at this timestamp, in one callback
            closed: list[Bar] = []
            for s in self.subs:
                closed.extend(self.builders[s.sub_id].bars_closed_by(tick))
            if closed:
                closed.sort(key=lambda b: (b.close_ts, b.sub_id))
                for b in closed:
                    self.history._append(b)
                self.n_bars += len(closed)
                ev = BarEvent(ts=tick.ts, seq=tick.seq, bars=tuple(closed))
                self._dispatch("bars", ev, tick, actionable)

            # the triggering tick belongs to the next bar
            for s in self.subs:
                self.builders[s.sub_id].add(tick)

            # lifecycle events raised while resolving are delivered last so the
            # strategy has already seen the fill that caused them
            self._drain_lifecycle(tick, actionable)

        if session is not None:
            self._close_session(session, self.n_ticks, actionable=session not in warm)

        self.blotter.final_position = self.position
        self.blotter.final_price = self.last_price

    # ---- sessions ---------------------------------------------------------- #
    def _open_session(self, session: int, tick: Tick) -> None:
        start, _ = self.dataset.session_bounds(session)
        for s in self.subs:
            self.builders[s.sub_id].start_session(session, start)
        self._emit(LifecycleKind.SESSION_OPEN, tick.ts, tick.seq)

    def _close_session(self, session: int, seq: int, *, actionable: bool) -> None:
        _, close_ts = self.dataset.session_bounds(session)
        tail = [b for s in self.subs if (b := self.builders[s.sub_id].flush_session())]
        if tail:
            tail.sort(key=lambda b: (b.close_ts, b.sub_id))
            for b in tail:
                self.history._append(b)
            self.n_bars += len(tail)
            self._dispatch(
                "bars",
                BarEvent(ts=close_ts, seq=seq, bars=tuple(tail)),
                Tick(close_ts, self.last_price, 0, session, seq),
                actionable,
            )

        # DAY cancels; GTD cancels if it expires at or before the close; GTC and
        # unexpired GTD survive, brackets included, and so does the position. A
        # strategy that wants to be flat overnight has to say so -- and the
        # end-of-session position is recorded so an unintended hold is visible.
        for order in list(self.orders.values()):
            if not order.is_working:
                continue
            if order.tif is TimeInForce.DAY:
                self._cancel(order, LifecycleKind.DAY_EXPIRED, close_ts, seq)
            elif order.tif is TimeInForce.GTD and order.expire_ts is not None:
                if order.expire_ts <= close_ts:
                    self._cancel(order, LifecycleKind.GTD_EXPIRED, close_ts, seq)

        self.end_of_session_positions[str(session)] = self.position
        self._emit(LifecycleKind.SESSION_CLOSE, close_ts, seq)
        self._drain_lifecycle(
            Tick(close_ts, self.last_price, 0, session, seq), actionable
        )

    # ---- order handling ----------------------------------------------------- #
    def _resolve(self, tick: Tick) -> list[FillEvent]:
        out: list[FillEvent] = []
        for order in list(self.orders.values()):
            if not order.is_working:
                continue
            if tick.seq < order.eligible_from_seq:
                continue  # the T+1 rule
            if order.status is OrderStatus.PENDING:
                order.status = OrderStatus.WORKING

            fill = self.model.resolve(order, tick)

            if fill is None:
                if order.tif in (TimeInForce.IOC, TimeInForce.FOK):
                    self._cancel(order, LifecycleKind.CANCEL_CONFIRMED, tick.ts, tick.seq)
                continue
            if order.tif is TimeInForce.FOK and fill.size < order.remaining:
                self._cancel(order, LifecycleKind.CANCEL_CONFIRMED, tick.ts, tick.seq)
                continue

            out.append(self._apply_fill(order, fill, tick))
            if order.tif is TimeInForce.IOC and order.remaining > 0:
                self._cancel(order, LifecycleKind.CANCEL_CONFIRMED, tick.ts, tick.seq)
        return out

    def _apply_fill(self, order: Order, fill: Fill, tick: Tick) -> FillEvent:
        signed = fill.size * order.side.sign
        realized = 0.0
        if self.position != 0 and (self.position > 0) != (signed > 0):
            closing = min(abs(signed), abs(self.position))
            realized = (fill.price - self._avg_price) * closing * (
                1 if self.position > 0 else -1
            )
            remaining_after = self.position + signed
            if (self.position > 0) == (remaining_after > 0) or remaining_after == 0:
                pass  # average price unchanged while reducing or flattening
            else:
                self._avg_price = fill.price  # flipped through zero
            self.position = remaining_after
        else:
            total = abs(self.position) + fill.size
            self._avg_price = (
                (self._avg_price * abs(self.position) + fill.price * fill.size) / total
                if total
                else fill.price
            )
            self.position += signed

        commission = self.commission * fill.size
        order.filled_size += fill.size
        if order.remaining == 0:
            order.status = OrderStatus.FILLED

        self.n_fills += 1
        self.blotter.add(
            BlotterRow(
                seq=tick.seq,
                ts=tick.ts,
                order_id=order.order_id,
                side=order.side,
                price=fill.price,
                size=fill.size,
                position_after=self.position,
                commission=commission,
                realized_pnl=realized,
                tag=self._order_tags.get(order.order_id, ""),
            )
        )

        # OCO first, before any callback: the sibling of a filled leg must not be
        # resolvable against this same tick.
        if order.oco_group is not None:
            self._cancel_oco_siblings(order, tick)
        if order.bracket is not None and order.status is OrderStatus.FILLED:
            self._arm_bracket(order, fill, tick)

        return FillEvent(
            ts=tick.ts,
            seq=tick.seq,
            order_id=order.order_id,
            side=order.side,
            price=fill.price,
            size=fill.size,
            position=self.position,
            last_trade_price=tick.price,
            working_order_ids=self._working_ids(),
        )

    def _arm_bracket(self, parent: Order, fill: Fill, tick: Tick) -> None:
        """Stop and target attached atomically at fill time.

        A strategy filled at 12:30:05 that has to wait for its next bar callback
        to place protection sits unprotected for the rest of the interval, and
        exits managed by strategy code is exactly where the historical fade
        artefact lived.
        """
        b: BracketSpec = parent.bracket
        exit_side = parent.side.opposite
        group = self._next_oco_group
        self._next_oco_group += 1
        if b.stop_price is not None or b.stop_offset is not None:
            stop_px = (
                b.stop_price
                if b.stop_price is not None
                else (
                    fill.price - b.stop_offset
                    if parent.side is Side.BUY
                    else fill.price + b.stop_offset
                )
            )
            self._new_order(
                Submit(
                    side=exit_side,
                    size=fill.size,
                    order_type=OrderType.STOP,
                    tif=TimeInForce.GTC,
                    stop_price=stop_px,
                    reduce_only=True,
                    tag=f"bracket-stop:{parent.order_id}",
                ),
                tick,
                oco_group=group,
                parent_id=parent.order_id,
            )
        if b.target_price is not None or b.target_offset is not None:
            tgt_px = (
                b.target_price
                if b.target_price is not None
                else (
                    fill.price + b.target_offset
                    if parent.side is Side.BUY
                    else fill.price - b.target_offset
                )
            )
            self._new_order(
                Submit(
                    side=exit_side,
                    size=fill.size,
                    order_type=OrderType.LIMIT,
                    tif=TimeInForce.GTC,
                    limit_price=tgt_px,
                    reduce_only=True,
                    tag=f"bracket-target:{parent.order_id}",
                ),
                tick,
                oco_group=group,
                parent_id=parent.order_id,
            )

    def _cancel_oco_siblings(self, filled: Order, tick: Tick) -> None:
        for other in self.orders.values():
            if (
                other.order_id != filled.order_id
                and other.oco_group == filled.oco_group
                and other.is_working
            ):
                self._cancel(other, LifecycleKind.OCO_CANCELLED, tick.ts, tick.seq)

    def _alloc_order_id(self) -> int:
        oid = self._next_order_id
        self._next_order_id += 1
        return oid

    def _new_order(
        self,
        s: Submit,
        tick: Tick,
        *,
        oco_group: int | None = None,
        parent_id: int | None = None,
        order_id: int | None = None,
    ) -> Order:
        oid = self._alloc_order_id() if order_id is None else order_id
        order = Order(
            order_id=oid,
            side=s.side,
            size=s.size,
            order_type=s.order_type,
            tif=s.tif,
            limit_price=s.limit_price,
            stop_price=s.stop_price,
            expire_ts=s.expire_ts,
            bracket=s.bracket,
            parent_id=parent_id,
            oco_group=oco_group,
            reduce_only=s.reduce_only,
            eligible_from_seq=tick.seq + 1,  # T+1, strictly after
            submitted_ts=tick.ts,
        )
        self.orders[oid] = order
        if s.tag:
            self._order_tags[oid] = s.tag
        self._emit(LifecycleKind.ORDER_ACCEPTED, tick.ts, tick.seq, oid)
        return order

    def _cancel(self, order: Order, kind: LifecycleKind, ts: Ts, seq: int) -> None:
        order.status = (
            OrderStatus.EXPIRED
            if kind in (LifecycleKind.DAY_EXPIRED, LifecycleKind.GTD_EXPIRED)
            else OrderStatus.CANCELLED
        )
        self._emit(kind, ts, seq, order.order_id)

    def _working_ids(self) -> tuple[int, ...]:
        return tuple(sorted(o.order_id for o in self.orders.values() if o.is_working))

    # ---- callbacks and actions ---------------------------------------------- #
    def _dispatch(self, kind: str, event, tick: Tick, actionable: bool) -> None:
        """Build the callback's context, run the callback, apply what it queued.

        The context is expired the moment the callback returns: a strategy that
        stashes it and reaches for it later would be reading state from an
        instant it is not standing in.
        """
        queue = IntentQueue(self.action_cap, self._alloc_order_id, live=actionable)
        common = dict(
            ts=tick.ts,
            seq=tick.seq,
            session=tick.session,
            fills=tuple(self._fills_since_callback),
            actionable=actionable,
        )
        if kind == "bars":
            ctx = BarContext(self, queue, event=event, **common)
            handler = self.strategy.on_bars
        elif kind == "fill":
            ctx = FillContext(self, queue, fill=event, **common)
            handler = self.strategy.on_fill
        else:
            ctx = LifecycleContext(self, queue, event=event, **common)
            handler = self.strategy.on_lifecycle

        try:
            handler(ctx)
        finally:
            ctx._expire()

        self._fills_since_callback = []
        if queue.cap_hit:
            # A ping-pong bug -- fill, modify, fill, modify -- otherwise hangs the
            # run and the wall-clock budget kills it with no record of why.
            # Dropping past the cap turns a mystery timeout into a counted number.
            self.action_cap_hits += 1
            self.actions_dropped += queue.dropped
        self.warmup_actions_ignored += queue.ignored
        if actionable:
            self._apply_actions(queue.actions, tick)

    def _apply_actions(
        self, actions: Sequence[tuple[Action, int | None]], tick: Tick
    ) -> None:
        for a, oid in actions:
            if isinstance(a, Submit):
                self._new_order(a, tick, order_id=oid)
            elif isinstance(a, Cancel):
                o = self.orders.get(a.order_id)
                if o is not None and o.is_working:
                    self._cancel(o, LifecycleKind.CANCEL_CONFIRMED, tick.ts, tick.seq)
                else:
                    self._emit(
                        LifecycleKind.ORDER_REJECTED, tick.ts, tick.seq, a.order_id,
                        "cancel for an order that is not working",
                    )
            elif isinstance(a, Modify):
                o = self.orders.get(a.order_id)
                if o is None or not o.is_working:
                    self._emit(
                        LifecycleKind.ORDER_REJECTED, tick.ts, tick.seq, a.order_id,
                        "modify for an order that is not working",
                    )
                    continue
                if a.limit_price is not None:
                    o.limit_price = a.limit_price
                if a.stop_price is not None:
                    o.stop_price = a.stop_price
                if a.size is not None:
                    o.size = a.size
                # A modified order is a new intent and re-enters the queue under
                # the same T+1 rule.
                o.eligible_from_seq = tick.seq + 1
            else:
                raise TypeError(f"unknown action {a!r}")

    def _emit(
        self,
        kind: LifecycleKind,
        ts: Ts,
        seq: int,
        order_id: int | None = None,
        detail: str = "",
    ) -> None:
        self._pending_events.append(LifecycleEvent(ts, seq, kind, order_id, detail))

    def _drain_lifecycle(self, tick: Tick, actionable: bool) -> None:
        while self._pending_events:
            batch, self._pending_events = self._pending_events, []
            for ev in batch:
                self._dispatch("lifecycle", ev, tick, actionable)

    def scored_sessions(self) -> list[int]:
        """The actionable sessions of the served split -- the observation unit
        the metric scores on. Warmup sessions are excluded: the strategy saw
        their bars but could not act, so they are not observations."""
        from .dataset import sessions_for

        _, live = sessions_for(self.dataset.calendar(), self.split, self.warmup_sessions)
        return list(live)

    def session_calendar(self):
        from .metrics import session_calendar

        return session_calendar(self.dataset, self.scored_sessions())

    # ---- record -------------------------------------------------------------- #
    def _make_record(self, outcome: str, error: str | None, wall: float) -> RunRecord:
        lock = getattr(self.dataset, "holdout_lock", None)
        return RunRecord(
            dataset=self.dataset.name,
            dataset_digest=self.dataset.digest(),
            split=str(self.split),
            fill_model=self.model.name,
            capabilities=self.dataset.capabilities().as_dict(),
            seed=self.seed,
            git_sha=git_sha(),
            wall_clock_s=round(wall, 6),
            subscriptions=[s.as_dict() for s in getattr(self, "subs", [])],
            action_cap=self.action_cap,
            action_cap_hits=self.action_cap_hits,
            actions_dropped=self.actions_dropped,
            warmup_actions_ignored=self.warmup_actions_ignored,
            ticks=self.n_ticks,
            bars_delivered=self.n_bars,
            fills=self.n_fills,
            outcome=outcome,
            error=error,
            warmup_sessions=self.warmup_sessions,
            end_of_session_positions=dict(self.end_of_session_positions),
            holdout_unlock_reason=(lock.reason or None) if lock else None,
            blotter_digest=self.blotter.digest(),
        )
