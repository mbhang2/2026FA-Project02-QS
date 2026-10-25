"""The event loop: the immutable side of the split.

Ordering within a tick, and why it is this order:

1. resolve resting orders against the tick  -> FillEvents
2. deliver fill callbacks                    (actionable)
3. deliver the tick event, if subscribed     (actionable)
4. deliver one BarEvent for every bar that closed at this timestamp (actionable)

Fills before everything because a fill is a fact about this tick, and a strategy
should learn about its own fill before being asked to react to the trade that
caused it. The tick before the bars because a bar is a summary of ticks that
ended before this one -- the tick that closes a bar belongs to the next bar, so
delivering it after the close would report it out of order. One BarEvent rather
than one per timeframe, because if a 5m and a 1m bar both close at 09:35 and
arrive separately, the ordering between them becomes a hidden variable that
affects results and is recorded nowhere.

Everything a strategy submits at tick T becomes eligible at T+1, strictly after.
Without that a strategy fills at the close of the bar it just saw, which is a
clean one-bar lookahead producing beautiful and entirely fake results.
"""

from __future__ import annotations

import inspect
import time

import numpy as np
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from .bars import BarBuilder
from .context import (
    BarContext,
    ContextError,
    EnginePort,
    FillContext,
    IntentQueue,
    LifecycleContext,
    SetupContext,
    TickContext,
    canonical_sub_id,
)
from ..data.dataset import Dataset
from .fills import FillModel, check_capabilities, get_fill_model
from .reconstruct import FillLogFull, log_digest, reconstruct
from ..runlog import RunNotAccounted, RunTicket
from ..types import (
    Action,
    Bar,
    BarEvent,
    Blotter,
    BlotterRow,
    BracketSpec,
    Cancel,
    Fill,
    FillEvent,
    FillRecord,
    LifecycleEvent,
    LifecycleKind,
    Modify,
    Order,
    OrderStatus,
    OrderType,
    OrderView,
    Side,
    Split,
    Strategy,
    Submit,
    Subscription,
    Tick,
    TickEvent,
    TickStrategy,
    TimeInForce,
    Ts,
)
from .view import BarHistory, HistoryView

DEFAULT_ACTION_CAP = 64

# The fill log is bounded, and the bound raises. 564 sessions of the reference
# strategy is about 34,000 fills; a strategy pinned to the action cap for all of
# them could queue far more, and the answer to that has to be an exception rather
# than a shorter log -- a truncated log is a wrong score with no error.
MAX_FILL_LOG = 5_000_000

# How often the walk looks at the clock. Every tick would be a syscall per tick.
# Counted in ticks *and* in callbacks, because a slow strategy is slow in
# callbacks: a tick-only counter lets a strategy sleeping in on_bars overshoot
# its budget several times over before anyone looks.
BUDGET_CHECK_EVERY_TICKS = 4096
BUDGET_CHECK_EVERY_CALLBACKS = 32


@dataclass(slots=True)
class _RunState:
    """The handful of live numbers a strategy may read, in one small record.

    It exists so the context's port can close over *this* rather than over the
    engine. A caller that walks `__closure__` looking for a way out finds six
    numbers.

    `realized_pnl` is accumulated rather than summed from the blotter on demand,
    for the same reason: reaching the blotter would reach a mutable list of rows,
    and a strategy that can append to the blotter can write its own result.
    """

    position: int = 0
    avg_price: float = 0.0
    last_price: float = 0.0
    realized_pnl: float = 0.0
    session_start: Ts = 0
    session_end: Ts = 0


class _OrderBook:
    """The working orders, and the two questions a context may ask about them.

    Narrow on purpose: the context's port binds `views` and `state_of`, so the
    closure reaches this rather than the engine. It still holds live `Order`
    objects, which is the one thing a determined caller could reach by walking
    `__self__` -- recorded in `docs/design/architecture.md` as a known residual,
    because in-process isolation is not achievable in Python and pretending
    otherwise is worse than saying so.
    """

    __slots__ = ("working", "all", "tags", "next_id")

    def __init__(self) -> None:
        self.working: dict[int, Order] = {}
        self.all: dict[int, Order] = {}
        self.tags: dict[int, str] = {}
        self.next_id = 1

    def allocate(self) -> int:
        """The id the next order will get.

        Here rather than on the engine because `IntentQueue` holds the allocator
        and the queue is reachable from the context: binding the engine's method
        put the engine one `__self__` away from strategy code. The reachability
        walk in `tests/test_boundary.py` found exactly that, after the rest of
        this refactor was done -- which is the argument for walking rather than
        reasoning.
        """
        oid = self.next_id
        self.next_id += 1
        return oid

    def ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.working))

    def views(self) -> tuple[OrderView, ...]:
        return tuple(
            OrderView.of(o, self.tags.get(oid, ""))
            for oid, o in self.working.items()
        )

    def state_of(self, order_id: int) -> tuple[bool, str] | None:
        order = self.all.get(order_id)
        if order is None:
            return None
        return order.is_working, str(order.status)


class _SubResolver:
    """Canonicalises a subscription id and refuses one that was not declared."""

    __slots__ = ("ids",)

    def __init__(self) -> None:
        self.ids: frozenset[str] | None = None

    def resolve(self, sub) -> str:
        sid = canonical_sub_id(sub)
        if self.ids is None:
            raise ContextError("subscriptions are not declared until setup() runs")
        if sid not in self.ids:
            raise ContextError(
                f"no subscription {sid!r}"
                + (f" (from {sub!r})" if str(sub) != sid else "")
                + f"; declare it in setup(). Subscribed: {sorted(self.ids)}"
            )
        return sid


class _SealedDataset:
    """The dataset with the holdout door taken off.

    The engine wraps whatever it is given, so the object it holds for the
    duration of a run has no `unlock_holdout` and no `holdout_lock` on it at all.
    Opening the holdout is then something only the caller that constructed the
    dataset can do, before the engine ever sees it -- a separate, audited path
    rather than a method sitting one attribute away from the walk.

    Belt and braces since Task 20 also removed the dataset from the context
    entirely. Kept because the two failures are different: one is a strategy
    reaching the dataset, the other is anything inside the engine calling the
    method by accident, and only the second survives the first fix.
    """

    __slots__ = ("_inner", "name", "holdout_unlock_reason")

    _FORWARD = (
        "capabilities", "calendar", "ticks", "session_bounds", "digest",
        "warmup_sessions", "scored_sessions", "early_close_dates", "load",
        "sessions", "periods", "spec", "contract_for",
        # Added 2026-10-24 with the options channel. An allowlist is the point
        # of this proxy, so a new channel has to be named here deliberately --
        # which is why the first options run failed with a bare
        # `AttributeError: options` rather than quietly working. What it returns
        # is a feed that turns a forward into quotes and holds nothing else, so
        # forwarding it does not widen what the engine can reach.
        "options",
    )

    def __init__(self, dataset) -> None:
        object.__setattr__(self, "_inner", dataset)
        self.name = dataset.name
        lock = getattr(dataset, "holdout_lock", None)
        self.holdout_unlock_reason = (lock.reason or None) if lock else None

    def __getattr__(self, name: str):
        if name in ("unlock_holdout", "holdout_lock", "_lock"):
            raise AttributeError(
                f"{name} is not available on the dataset the engine holds. "
                "Opening the holdout is a deliberate act by whoever constructs "
                "the dataset, recorded in the run record, and not something "
                "reachable from inside a run."
            )
        if name in self._FORWARD:
            return getattr(self._inner, name)
        raise AttributeError(name)


class BudgetExceeded(RuntimeError):
    """The run outlived its wall-clock budget and was abandoned.

    The action cap protects against a ping-pong bug; it does not protect against
    an O(n^2) feature recomputation, which is the likelier failure once a
    strategy computes features from raw ticks. A nightly loop of hundreds of runs
    becomes a nightly loop of three if one strategy can spin.
    """

    def __init__(self, budget_s: float, elapsed_s: float, ticks: int):
        self.budget_s, self.elapsed_s, self.ticks = budget_s, elapsed_s, ticks
        super().__init__(
            f"abandoned after {elapsed_s:.1f}s against a {budget_s:g}s budget "
            f"({ticks:,} ticks in)"
        )


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
    ticks_stepped: int
    tick_events_delivered: int
    bars_delivered: int
    fills: int
    outcome: str
    error: str | None
    warmup_sessions: int
    wall_clock_budget_s: float | None
    end_of_session_positions: dict[str, int]
    holdout_unlock_reason: str | None
    blotter_digest: str
    fill_log_digest: str = ""
    fills_logged: int = 0
    reconciliation: dict | None = None
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


def _callbacks_of(proto) -> tuple[str, ...]:
    """The callback names a Protocol declares itself, in declaration order.

    `vars()` rather than `dir()`, so `TickStrategy` reports only `on_tick` and
    not the four it inherits -- the two sets are used separately.
    """
    return tuple(
        name for name, obj in vars(proto).items()
        if not name.startswith("_") and callable(obj)
    )


REQUIRED_CALLBACKS = _callbacks_of(Strategy)
TICK_CALLBACKS = _callbacks_of(TickStrategy)

# `fit()` takes nothing -- it reads what the strategy accumulated during the fit
# window -- while the weight methods take a path. So they cannot go through
# `validate_strategy`, whose arity check and whose error message are both
# specifically about "exactly one argument (the context)".
# `save_weights()` takes nothing and RETURNS bytes; `load_weights(blob)` takes
# them. Not a path on either side: the strategy runs in a child process that is
# denied `file-write*`, so a strategy asked to write its own weights could not.
# The parent owns every write, which is also what makes the recorded digest
# describe exactly what crossed. See `docs/design/ml-strategies.md` section 11.
FIT_ARITY: dict[str, int] = {"fit": 0, "save_weights": 0, "load_weights": 1}

_MISSING = object()


def validate_strategy(strategy: Any, *, needs_ticks: bool = False) -> None:
    """Check the loaded strategy against `types.Strategy` before the walk starts.

    One error listing everything wrong, raised at wiring time. Previously
    `on_tick` was checked with `hasattr` and produced a clear message, while
    `on_fill` and `on_lifecycle` were reached by attribute access mid-run and
    produced an `AttributeError` at the first session open -- three callbacks,
    two failure modes, one of them unreadable. The protocol is the declaration
    and this is what makes it load-bearing rather than decorative.

    Signatures are checked too, because the shape changed once already: the
    protocol still declared the pre-Task-5 `(ev, ctx)` form four tasks after the
    engine stopped calling it that way.
    """
    wanted = list(REQUIRED_CALLBACKS)
    if needs_ticks:
        wanted += [n for n in TICK_CALLBACKS if n not in wanted]

    problems: list[str] = []
    for name in wanted:
        fn = getattr(strategy, name, None)
        if fn is None:
            problems.append(f"  {name}(ctx) is missing")
            continue
        if not callable(fn):
            problems.append(f"  {name} is {type(fn).__name__}, not a method")
            continue
        try:
            inspect.signature(fn).bind(object())
        except TypeError as exc:
            problems.append(
                f"  {name}{inspect.signature(fn)} does not take exactly one "
                f"argument (the context): {exc}"
            )
    if problems:
        raise TypeError(
            f"{type(strategy).__name__} does not match the strategy contract:\n"
            + "\n".join(problems)
            + "\n\nEvery callback takes the context and returns nothing. See "
            "docs/agent/strategy-api.md."
        )


def validate_fitted_strategy(strategy: Any) -> None:
    """Check a fitted strategy against `types.FittedStrategy` before the walk.

    Separate from `validate_strategy` rather than a flag on it, because the arity
    differs -- `fit()` takes nothing and the weight methods take a path -- and
    because that function's error message is written about callbacks taking the
    context. A strategy must pass both.

    Design: `docs/design/ml-strategies.md`.
    """
    problems: list[str] = []
    for name, arity in FIT_ARITY.items():
        fn = getattr(strategy, name, _MISSING)
        if fn is _MISSING:
            # **`fit()` is what makes a strategy fitted; the weight methods are
            # how one is carried between runs.** Requiring all three made a class
            # with `fit()` and no `save_weights` read as ordinary: the fit window
            # was never carved out, `fit()` was never called, and the model was
            # scored with whatever its constructor left behind -- silently, and
            # the row looked real. It also barred `RemoteFittedStrategy`, which
            # forwards `fit` and deliberately not the weight methods, because a
            # campaign fits and scores inside one run and persists nothing.
            #
            # So: `fit` is required, the weight methods are validated when they
            # exist, and `load_weights` is required only by a run that was given
            # weights to load -- checked at the load site, where the path is.
            if name != "fit":
                continue
            problems.append(f"  {name}() is missing")
            continue
        # Declared but unusable is always an error, even for the optional two.
        # `load_weights = None` is a typo or an abandoned stub, not a considered
        # decision to omit the method, and the distinction is the whole reason
        # this reads through a sentinel rather than through `getattr(..., None)`.
        if not callable(fn):
            problems.append(f"  {name} is {type(fn).__name__}, not a method")
            continue
        try:
            inspect.signature(fn).bind(*(object(),) * arity)
        except TypeError as exc:
            problems.append(
                f"  {name}{inspect.signature(fn)} does not take exactly "
                f"{arity} argument(s): {exc}")
    if problems:
        raise TypeError(
            f"{type(strategy).__name__} declared itself fitted but does not match "
            "the contract:\n" + "\n".join(problems)
            + "\n\nSee docs/design/ml-strategies.md."
        )


def aggregate(ticks: Iterable[Tick], sub: Subscription, bounds: Any) -> list[Bar]:
    """Bars for a tick stream, session-anchored, using the live builder.

    `bounds` is `session -> (open_ts, close_ts)`. A callable rather than the
    dataset, because `SetupContext` may not hold a dataset: a strategy that could
    reach it could open the holdout.
    """
    if hasattr(bounds, "session_bounds"):
        bounds = bounds.session_bounds
    builder = BarBuilder(sub)
    out: list[Bar] = []
    session = None
    for t in ticks:
        if t.session != session:
            if session is not None:
                tail = builder.flush_session(bounds(session)[1])
                if tail is not None:
                    out.append(tail)
            session = t.session
            builder.start_session(session, bounds(session)[0])
        out.extend(builder.bars_closed_by(t))
        builder.add(t)
    if session is not None:
        tail = builder.flush_session(bounds(session)[1])
        if tail is not None:
            out.append(tail)
    return out


class Engine:
    """The walk. Construction requires a `RunTicket`.

    Until Task 23 "every execution is logged" was a convention that held because
    everyone remembered. The ledger is the denominator of every false-discovery
    statement the project makes, so a new entry point that forgot to append a row
    would have made N quietly wrong. `RunTicket` is minted by `LedgerRun` (which
    appends a row on exit, whatever happened), by `RunTicket.for_calibration`
    (measuring the apparatus with the immutable reference strategy -- one row per
    tabulation, not per engine), or by `RunTicket.for_test`, which is asserted
    absent from `src/` and `scripts/`.

    It stops accidents rather than attackers -- anything in this process can
    forge a ticket, as it can reach anything else in the address space. Use
    `Engine.for_test(...)` in fixtures; `tests/test_boundary.py` walks the tree
    for construction sites so a fourth route cannot appear quietly.
    """

    def __init__(
        self,
        dataset: Any,
        strategy: Any,
        *,
        ticket: RunTicket,
        split: Split = Split.VALIDATION,
        fill_model: FillModel | str = "unfavorable",
        warmup_sessions: int = 0,
        # The first `fit_sessions` of the split are accumulation: bars delivered,
        # actions ignored, excluded from the score. Only a fitted strategy may
        # set it. See `docs/design/ml-strategies.md`.
        fit_sessions: int = 0,
        # Weights to load instead of fitting. This is how a validation or holdout
        # run carries a champion: it loads what the training run produced rather
        # than refitting on the split it is about to be scored against.
        weights_path: Any = None,
        # Price units are the dataset's own. The synthetic generator works in
        # units where a bracket is ~0.75, so the default commission is scaled to
        # be a small fraction of a target rather than larger than one -- a cost
        # that exceeds the target makes every strategy a loser and hides whether
        # the harness can see an edge at all. A real adapter passes its own.
        commission_per_contract: float = 0.01,
        action_cap: int = DEFAULT_ACTION_CAP,
        seed: int | None = None,
        wall_clock_budget_s: float | None = None,
        # Steps every tick instead of jumping between events. The two paths must
        # produce identical blotters -- that is what the differential tests
        # assert -- and this switch is how they are compared. It is not a tuning
        # knob: the fast path is not an approximation.
        event_walk: bool = True,
    ):
        if not isinstance(ticket, RunTicket):
            raise RunNotAccounted(
                "Engine(...) needs a RunTicket, so that no run can happen without "
                "being accounted for. Use `with LedgerRun(...) as ticket:` on a "
                "scored path, `RunTicket.for_calibration(reason)` when measuring "
                "the apparatus, or `Engine.for_test(...)` in a fixture."
            )
        self.ticket = ticket
        self.dataset = _SealedDataset(dataset)
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
        self.wall_clock_budget_s = wall_clock_budget_s
        self.event_walk = event_walk
        self._deadline: float | None = None
        self._callbacks = 0
        self._next_budget_seq = BUDGET_CHECK_EVERY_TICKS
        self._session_seq0 = 0

        self.blotter = Blotter()
        # The score's source of truth. On the engine and nowhere else: not on
        # `_RunState`, not on `_OrderBook`, not closed over by anything in
        # `EnginePort`. The engine is not reachable from a context, so neither is
        # this, and the reachability walk asserts it.
        self._fill_log: list[FillRecord] = []
        self._state = _RunState()
        self._book = _OrderBook()
        self._resolver = _SubResolver()
        self.orders = self._book.all
        # Only the orders that can still do something, in creation order. The
        # resolver used to scan `orders` and test `is_working` on each, which
        # made every tick cost O(orders ever placed) -- quadratic in session
        # length, and on a real NQ session that was 35 million `is_working` calls
        # for 388,000 ticks. Insertion order matches `orders`, so the sequence
        # the resolver sees is unchanged.
        self._working = self._book.working
        self._next_oco_group = 1
        self._pending_events: list[LifecycleEvent] = []
        # The current session's bounds, so a strategy can be flat before the
        # close. An exchange calendar is published in advance, so knowing when
        # today ends is not lookahead -- and without it a strategy cannot be
        # reliably flat at a boundary, which cost the calibration apparatus a
        # working exit rule for three tasks running.

        self._order_tags = self._book.tags

        self.action_cap_hits = 0
        self.actions_dropped = 0
        self.warmup_actions_ignored = 0

        # A fitted strategy accumulates a prefix of the split and fits at its edge.
        #
        # **Not the warmup window, though the first draft of this used it.**
        # `sessions_for` returns `(range(start, b.start), range(b.start, b.stop))`:
        # warmup is the sessions *before* the split, borrowed as context. TRAIN is
        # the first split, so it has none -- `warmup_sessions(TRAIN, 10)` is empty,
        # and campaigns run on TRAIN. Using warmup as the fit window would have
        # fitted on nothing there, and on data outside the split everywhere else.
        #
        # So the fit window is its own thing: the first `fit_sessions` of the
        # split. It behaves like warmup in the two ways that matter -- bars are
        # delivered, actions are ignored -- and, like warmup, it is excluded from
        # `scored_sessions()`. That exclusion is what stops Amendment 3's idle
        # charge billing every session the strategy was required not to trade in.
        self.fit_sessions = fit_sessions
        # Kept so the run record can say which weights file was loaded
        # rather than re-deriving it from the profile at write time.
        self.weights_path = weights_path
        self._fit_cache: set[int] | None = None
        self._warm: set[int] = set()
        # `fit` alone, not all three -- see `validate_fitted_strategy`.
        self._needs_fit = hasattr(strategy, "fit")
        self._has_fit = False
        # `_has_fit` is set BEFORE `fit()` is called, so that a re-entrant call
        # raises; `_fit_completed` is set only AFTER it returns. The distinction
        # is what `run.py`'s `attach_weights` needs: a strategy that raised
        # inside `fit()` has `_has_fit` and no weights, and asking its dead
        # child to `save_weights()` from inside the crash handler raised a
        # second exception past every handler -- a row with no outcome, found
        # 2026-09-25 by running a fitted strategy on a synthetic window too
        # short for it.
        self._fit_completed = False
        self._loaded_weights = False
        if self._needs_fit:
            validate_fitted_strategy(strategy)
            if weights_path is not None:
                # **The load path, and the reason section 2a exists.**
                # `--stage validate` carries a winner to the validation split by
                # writing its SOURCE and running it. A fitted strategy that fitted
                # there would train on the split it is about to be scored against,
                # silently and flatteringly. So a run given weights loads them and
                # is marked already-fitted: `_fit_now` then refuses, and there is
                # no fit window to reach in the first place.
                from pathlib import Path as _P

                wp = _P(str(weights_path))
                if not wp.exists():
                    raise FileNotFoundError(
                        f"weights_path {wp} does not exist. A run that needs "
                        "weights must load them; a missing file is an error and "
                        "not a licence to refit on this split. See "
                        "docs/design/ml-strategies.md."
                    )
                if not hasattr(strategy, "load_weights"):
                    # The one run that genuinely requires the method, so the one
                    # place it is required. A proxy that forwards only `fit`
                    # reaches here exactly when a profile asks it to carry a
                    # champion's weights across splits, which is phase 2.
                    raise TypeError(
                        f"weights_path {wp} was given but the strategy defines "
                        "no load_weights(). A run cannot carry weights it has no "
                        "way to read. See docs/design/ml-strategies.md section 11."
                    )
                strategy.load_weights(wp.read_bytes())
                self._loaded_weights = True
                self._has_fit = True
                self._fit_completed = True
                if fit_sessions:
                    raise TypeError(
                        "weights_path and fit_sessions were both given. A run "
                        "either fits or loads; doing both would discard the loaded "
                        "weights and retrain on this split."
                    )
            elif self.fit_sessions <= 0:
                raise TypeError(
                    f"{type(strategy).__name__} is a fitted strategy but "
                    "fit_sessions=0 and no weights_path was given, so it would "
                    "neither fit nor load. It would trade on an unfitted model, "
                    "which is a silently meaningless run. See "
                    "docs/design/ml-strategies.md."
                )
        elif fit_sessions:
            raise TypeError(
                f"fit_sessions={fit_sessions} was given but "
                f"{type(strategy).__name__} is not a fitted strategy, so the "
                "window would remove sessions from the score for no reason."
            )
        self._fills_since_callback: list = []
        self.tick_history: HistoryView = HistoryView()
        self._tick_subscribed = False
        self.n_tick_events = 0
        self.n_ticks = 0
        # Ticks the walk actually stopped at. n_ticks - n_stepped is what the
        # jump saved, and it is reported so the speedup is a measurement rather
        # than a claim.
        self.n_stepped = 0
        self.n_bars = 0
        self.n_fills = 0
        self.end_of_session_positions: dict[str, int] = {}

    @classmethod
    def for_test(cls, dataset: Any, strategy: Any, **kw) -> "Engine":
        """A fixture's route to an engine, and the only one that logs nothing.

        Separate from an optional ticket on purpose: an optional argument is one
        a production caller can also omit, and then the guarantee is back to
        being a habit. A test asserts this name appears nowhere under `src/` or
        `scripts/`.
        """
        return cls(dataset, strategy, ticket=RunTicket.for_test(), **kw)

    # `position`, `last_price` and `_avg_price` live on `_RunState` so the
    # context's port can close over that record rather than over the engine.
    # Properties keep every existing call site working.
    @property
    def position(self) -> int:
        return self._state.position

    @position.setter
    def position(self, value: int) -> None:
        self._state.position = value

    @property
    def last_price(self) -> float:
        return self._state.last_price

    @last_price.setter
    def last_price(self, value: float) -> None:
        self._state.last_price = value

    @property
    def _avg_price(self) -> float:
        return self._state.avg_price

    @_avg_price.setter
    def _avg_price(self, value: float) -> None:
        self._state.avg_price = value

    @property
    def _session_start(self) -> Ts:
        return self._state.session_start

    @property
    def _session_end(self) -> Ts:
        return self._state.session_end

    def _make_port(self) -> "EnginePort":
        """Bind each operation to the narrowest object that can answer it.

        Nothing here closes over `self`. That is the whole point: a context holds
        this record of callables and no engine, so there is no engine at the end
        of any attribute path from a strategy.
        """
        state, book, resolver = self._state, self._book, self._resolver
        history, ticks = self.history, self.tick_history
        return EnginePort(
            position=lambda: state.position,
            last_price=lambda: state.last_price,
            average_entry_price=lambda: state.avg_price,
            realized_pnl=lambda: state.realized_pnl,
            session_bounds=lambda: (state.session_start, state.session_end),
            working_ids=book.ids,
            order_views=book.views,
            order_state=book.state_of,
            resolve_sub=resolver.resolve,
            history=history.__getitem__,
            tick_history=lambda: ticks,
            options_snapshot=self._options_snapshot_fn(),
        )

    def _options_snapshot_fn(self):
        """A callable that answers with the surface, or None if there is none.

        Closes over the FEED and over `state`, never over the engine -- the same
        rule every other port entry follows. The feed only knows how to turn a
        forward into quotes, so nothing reachable from here can advance the
        walk, read the split or touch the holdout lock.
        """
        caps = self.dataset.capabilities()
        if not getattr(caps, "has_options", False):
            return None
        feed = self.dataset.options()
        state = self._state
        # **The instant comes from the callback, the forward from the state.**
        # `_RunState` carries no `ts` or `session` -- those are per-callback and
        # live on the context -- so the first version closed over `state.ts` and
        # died with `AttributeError: '_RunState' object has no attribute 'ts'`
        # inside the strategy's own try block, where it read as a strategy bug.
        # Passing them in keeps each value sourced from the thing that owns it.
        return lambda ts, session: tuple(
            feed.snapshot(ts, session, state.last_price))

    # ---- run --------------------------------------------------------------- #
    def run(self) -> RunRecord:
        started = time.monotonic()
        outcome, error = "completed", None
        self._deadline = (
            started + self.wall_clock_budget_s if self.wall_clock_budget_s else None
        )
        try:
            self._run_inner()
        except BudgetExceeded as exc:
            # An abandoned run is a trial and must be counted. It is not a crash:
            # nothing went wrong except that the strategy was too slow, and the
            # distinction matters when reading the ledger.
            outcome, error = "abandoned_on_budget", str(exc)
        except Exception as exc:  # a crash is a ledger row, not a lost trial
            outcome, error = "crashed", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.record = self._make_record(
                outcome, error, time.monotonic() - started
            )
        return self.record

    # ---- what the context resolves through --------------------------------- #
    def resolve_sub(self, sub) -> str:
        """Canonical id for a subscription the strategy declared. See
        `_SubResolver`: the logic lives there so the context's port can bind it
        without binding the engine."""
        return self._resolver.resolve(sub)

    def order_views(self) -> tuple[OrderView, ...]:
        return self._book.views()

    def _run_inner(self) -> None:
        warm = self._warm = set(
            self.dataset.warmup_sessions(self.split, self.warmup_sessions))

        # A factory, not a list. See SetupContext: eagerly materialising the
        # training split costs nothing on synthetic data and is on the order of
        # 22 GiB on the real one, for a strategy that may never read a tick of it.
        #
        # Bound to the *sealed* dataset in a local, not to `self`. A lambda over
        # `self` would have put the engine in its closure cell, which is exactly
        # what the reachability walk in tests/test_boundary.py found after the
        # rest of this refactor was done.
        sealed = self.dataset
        train = (
            (lambda: sealed.ticks(Split.TRAIN))
            if self.split is not Split.TRAIN
            else (lambda: iter(()))
        )
        # Before setup() runs, so a missing on_fill is a wiring error rather than
        # an AttributeError at the first session open. The tick callback is
        # checked again below, once the subscriptions are known.
        validate_strategy(self.strategy)

        # The dataset's name and one bound accessor, never the dataset.
        setup_ctx = SetupContext(
            self.dataset.name, self.dataset.session_bounds, train
        )
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
        # ticks first, then bars by interval: a stable order for the record, and
        # the delivery order is pinned separately in the walk below
        subs.sort(key=lambda s: (s.interval_ns is not None, s.interval_ns or 0, s.sub_id))
        self.subs = subs
        bar_subs = [s for s in subs if s.is_bars]
        self._tick_subscribed = any(not s.is_bars for s in subs)
        if self._tick_subscribed:
            # "the strategy subscribed to ticks but has no on_tick(ctx) callback"
            # was the only legible callback error; it is now one case of the
            # general check rather than a special one.
            validate_strategy(self.strategy, needs_ticks=True)
        self._resolver.ids = frozenset(s.sub_id for s in subs)
        self.builders = {s.sub_id: BarBuilder(s) for s in bar_subs}
        self.history = BarHistory([s.sub_id for s in bar_subs])
        self._port = self._make_port()

        session = None
        seq = -1
        for batch in self._sessions_of(
            self.dataset.ticks(self.split, warmup=self.warmup_sessions)
        ):
            if session is not None:
                self._close_session(session, seq + 1,
                                    actionable=self._is_actionable(session))
            session = batch[0].session
            actionable = self._is_actionable(session)
            if actionable and self._needs_fit and not self._has_fit:
                # The edge of the warmup window, crossed exactly once. Everything
                # before it was accumulation with actions ignored; everything after
                # is scored. `fit()` is called here and refused afterwards -- see
                # `_fit_now` for why that refusal is load-bearing.
                self._fit_now()
            self._open_session(session, Tick(batch[0].ts, batch[0].price,
                                             batch[0].size, session, seq + 1,
                                             batch[0].side))

            self._session_seq0 = seq + 1
            price = np.fromiter((t.price for t in batch), dtype=float, count=len(batch))
            tsarr = np.fromiter((t.ts for t in batch), dtype=np.int64, count=len(batch))
            sizes = np.fromiter((t.size for t in batch), dtype=np.int64, count=len(batch))
            # The aggressor, encoded so a skipped span's signed volume is one
            # vectorised pass rather than a Python loop: +1 buy, -1 sell, 0 for
            # a feed that did not say. Built here beside price/ts/size because
            # `add_span` needs it and the per-tick path gets it from `Tick.side`.
            sides = np.fromiter(
                (1 if t.side is Side.BUY else (-1 if t.side is Side.SELL else 0)
                 for t in batch), dtype=np.int8, count=len(batch))
            n = len(batch)

            i = 0
            while i < n:
                seq += 1
                src = batch[i]
                tick = Tick(src.ts, src.price, src.size, session, seq, src.side)
                self.n_ticks += 1
                self.n_stepped += 1
                if self._deadline and seq >= self._next_budget_seq:
                    self._next_budget_seq = seq + BUDGET_CHECK_EVERY_TICKS
                    self._check_budget()
                self._step(tick, actionable)

                nxt = self._next_event(i, n, tsarr, price)
                if nxt > i + 1:
                    # Nothing can happen in (i, nxt): no boundary is crossed and
                    # no resting level can be reached, so the only work is bar
                    # accumulation and it folds in one go.
                    lo, hi = i + 1, nxt
                    seg = price[lo:hi]
                    segs, segd = sizes[lo:hi], sides[lo:hi]
                    # Signed volume over the same slice, the same way. Unknown
                    # aggressors are in neither sum, so `Bar.unknown_volume`
                    # accounts for them -- the alternative is a delta that is
                    # silently wrong on exactly the ticks the feed was unsure
                    # about.
                    buy = int(segs[segd == 1].sum())
                    sell = int(segs[segd == -1].sum())
                    for builder in self.builders.values():
                        builder.add_span(
                            high=float(seg.max()), low=float(seg.min()),
                            close=float(seg[-1]), volume=int(segs.sum()),
                            count=hi - lo, first=float(seg[0]),
                            buy=buy, sell=sell,
                        )
                    skipped = hi - lo
                    self.n_ticks += skipped
                    seq += skipped
                    self.last_price = float(seg[-1])
                i = nxt

        if session is not None:
            self._close_session(session, self.n_ticks,
                                actionable=self._is_actionable(session))

        self.blotter.final_position = self.position
        self.blotter.final_price = self.last_price

    def _next_event(self, i: int, n: int, tsarr, price) -> int:
        """The next index at which anything can happen. Never later than the truth.

        Two candidates, both computable without stepping:

        * the next bar boundary, from a search over the timestamps;
        * the first tick that could reach any resting level, from a search over
          the prices.

        The price conditions here are deliberately **weaker** than the fill
        model's -- `<=` where `unfavorable` needs `<`, and the trigger condition
        of a stop-limit that has not armed. That one-sidedness is the safety
        property: the walk may stop somewhere nothing happens, and cannot skip
        somewhere something would have. The fill model is still the only thing
        that decides a fill.

        A tick subscription disables all of it: every tick must be delivered, and
        that is the point -- the cost of tick granularity is visible in the
        wall-clock budget rather than hidden.
        """
        if self._tick_subscribed or not self.event_walk:
            return i + 1
        # the last tick of a session is always an event, so `last_price` and the
        # session tail are exactly what a full walk would leave behind
        cand = n - 1 if i < n - 1 else n

        boundary = min(
            (b.current_boundary for b in self.builders.values()), default=None
        )
        if boundary is not None:
            cand = min(cand, int(np.searchsorted(tsarr, boundary, side="left")))
        if cand <= i + 1:
            return i + 1

        for order in self._working.values():
            start = max(i + 1, order.eligible_from_seq - (self._seq_base(i)))
            start = max(start, i + 1)
            if start >= cand:
                continue
            if order.tif in (TimeInForce.IOC, TimeInForce.FOK):
                return start          # decided on its first eligible tick
            if order.order_type is OrderType.MARKET:
                return start
            if order.expire_ts is not None:
                cand = min(cand, int(np.searchsorted(tsarr, order.expire_ts, side="left")))
                if cand <= start:
                    return max(start, cand)
            level, upward = self._trigger(order)
            if level is None:
                return start
            seg = price[start:cand]
            if seg.size == 0:
                continue
            hit = seg >= level if upward else seg <= level
            if hit.any():
                cand = start + int(hit.argmax())
                if cand <= i + 1:
                    return i + 1
        return max(cand, i + 1)

    def _seq_base(self, i: int) -> int:
        """`eligible_from_seq` is in global sequence; the arrays are per session."""
        return self._session_seq0

    @staticmethod
    def _trigger(order: Order) -> tuple[float | None, bool]:
        """(price level, whether it is reached from below) for a resting order.

        `None` means "cannot be expressed as a level" and the caller stops at the
        order's next eligible tick instead of trying to be clever.
        """
        if order.order_type is OrderType.LIMIT:
            if order.limit_price is None:
                return None, False
            return order.limit_price, order.side is Side.SELL
        if order.order_type is OrderType.STOP:
            if order.stop_price is None:
                return None, False
            return order.stop_price, order.side is Side.BUY
        if order.order_type is OrderType.STOP_LIMIT:
            if order.stop_armed:
                if order.limit_price is None:
                    return None, False
                return order.limit_price, order.side is Side.SELL
            if order.stop_price is None:
                return None, False
            return order.stop_price, order.side is Side.BUY
        return None, False

    def _sessions_of(self, ticks):
        """Group the tick stream into sessions without materialising the whole
        split. A session is the unit the walk vectorises over."""
        batch: list[Tick] = []
        current = None
        for t in ticks:
            if current is None:
                current = t.session
            elif t.session != current:
                yield batch
                batch, current = [], t.session
            batch.append(t)
        if batch:
            yield batch

    def _step(self, tick: Tick, actionable: bool) -> None:
        """Everything that happens at one tick. Unchanged in order and content;
        the walk simply calls it only where something can happen."""
        self.last_price = tick.price

        # 1. resolve resting orders against this tick
        fills = self._resolve(tick)

        # 2. fill callbacks
        for ev in fills:
            self._fills_since_callback.append(ev)
            self._dispatch("fill", ev, tick, actionable)

        # 3. the tick itself, before any bar it closes
        if self._tick_subscribed:
            ev = TickEvent(
                ts=tick.ts, seq=tick.seq, price=tick.price, size=tick.size,
                side=tick.side, session=tick.session,
            )
            self.tick_history._append(ev)
            self.n_tick_events += 1
            self._dispatch("tick", ev, tick, actionable)

        # 4. bars that closed at this timestamp, in one callback
        closed: list[Bar] = []
        for s in self.builders.values():
            closed.extend(s.bars_closed_by(tick))
        if closed:
            closed.sort(key=lambda b: (b.close_ts, b.sub_id))
            for b in closed:
                self.history._append(b)
            self.n_bars += len(closed)
            # One event per *close*, not per tick. A single tick after a quiet
            # stretch closes several intervals at once, and they did not close
            # together -- a 1m bar ending at 09:31 and another ending at 09:32
            # are two instants, delivered in the order they happened. Lumping
            # them into one event labelled with the tick's timestamp made
            # `ctx.bars` claim two closes at the same moment and put a bar in
            # history alongside one that closed a minute later.
            #
            # Bars that really do close together still arrive together, which is
            # the property the single-callback rule exists to protect: the
            # ordering between a 5m and a 1m close at the same instant stays out
            # of the strategy's hands.
            for bar_close in sorted({b.close_ts for b in closed}):
                group = tuple(b for b in closed if b.close_ts == bar_close)
                self._dispatch(
                    "bars",
                    BarEvent(ts=bar_close, seq=tick.seq, bars=group),
                    tick,
                    actionable,
                )

        # the triggering tick belongs to the next bar
        for builder in self.builders.values():
            builder.add(tick)

        # lifecycle events raised while resolving are delivered last so the
        # strategy has already seen the fill that caused them
        self._drain_lifecycle(tick, actionable)

    # ---- sessions ---------------------------------------------------------- #
    def _open_session(self, session: int, tick: Tick) -> None:
        start, end = self.dataset.session_bounds(session)
        self._state.session_start, self._state.session_end = start, end
        for builder in self.builders.values():
            builder.start_session(session, start)
        self._emit(LifecycleKind.SESSION_OPEN, tick.ts, tick.seq)

    def _close_session(self, session: int, seq: int, *, actionable: bool) -> None:
        _, close_ts = self.dataset.session_bounds(session)
        tail = [b for bl in self.builders.values() if (b := bl.flush_session(close_ts))]
        if tail:
            tail.sort(key=lambda b: (b.close_ts, b.sub_id))
            for b in tail:
                self.history._append(b)
            self.n_bars += len(tail)
            # Grouped by close, not lumped together. A session flush can produce
            # bars that closed at genuinely different instants -- a 1m interval
            # that completed before the last tick arrived, and a 5m interval
            # truncated by the session end -- and the whole point of one event
            # per timestamp is that the bars inside it closed together. Putting
            # two different closes in one event makes the same ordering a hidden
            # variable that the rule exists to remove. Invisible while every
            # session was an exact multiple of every interval.
            for bar_close in sorted({b.close_ts for b in tail}):
                group = tuple(b for b in tail if b.close_ts == bar_close)
                self._dispatch(
                    "bars",
                    BarEvent(ts=bar_close, seq=seq, bars=group),
                    Tick(bar_close, self.last_price, 0, session, seq),
                    actionable,
                )

        # DAY cancels; GTD cancels if it expires at or before the close; GTC and
        # unexpired GTD survive, brackets included, and so does the position. A
        # strategy that wants to be flat overnight has to say so -- and the
        # end-of-session position is recorded so an unintended hold is visible.
        for order in list(self._working.values()):
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
        for order in list(self._working.values()):
            if tick.seq < order.eligible_from_seq:
                continue  # the T+1 rule
            if (
                order.tif is TimeInForce.GTD
                and order.expire_ts is not None
                and tick.ts >= order.expire_ts
            ):
                # Until Task 7 this was only checked at a session boundary, so a
                # GTD order expiring mid-session rested on regardless -- accepted,
                # carried, and quietly not done.
                self._cancel(order, LifecycleKind.GTD_EXPIRED, tick.ts, tick.seq)
                continue
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

            event = self._apply_fill(order, fill, tick)
            if event is None:
                continue
            out.append(event)
            if order.tif is TimeInForce.IOC and order.remaining > 0:
                self._cancel(order, LifecycleKind.CANCEL_CONFIRMED, tick.ts, tick.seq)
        return out

    def _apply_fill(self, order: Order, fill: Fill, tick: Tick) -> FillEvent:
        # Revalidated here, not only at submit. `reduce_only` was checked when
        # the order was queued and never again, so two `ctx.close()` calls
        # against a long went +1 -> 0 -> -1 with both orders marked reduce-only:
        # the second was legitimate when it was written and had become an
        # opening order by the time it filled. Clamp to what is left, cancel the
        # remainder, and emit the clamp so it is visible rather than silent.
        if order.reduce_only:
            room = abs(self.position) if (
                self.position != 0
                and (self.position > 0) != (order.side is Side.BUY)
            ) else 0
            if room <= 0:
                self._cancel(order, LifecycleKind.ORDER_REJECTED, tick.ts, tick.seq)
                self._emit(
                    LifecycleKind.ORDER_REJECTED, tick.ts, tick.seq, order.order_id,
                    f"reduce_only {order.side} would open against a position of "
                    f"{self.position} by the time it filled",
                )
                return None
            if fill.size > room:
                self._emit(
                    LifecycleKind.ORDER_ACCEPTED, tick.ts, tick.seq, order.order_id,
                    f"reduce_only fill clamped from {fill.size} to {room} at fill "
                    "time; the position had already shrunk",
                )
                fill = Fill(price=fill.price, size=room)

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
        # Accumulated here rather than summed from the blotter on demand, so the
        # context can read it without the blotter being reachable -- a mutable
        # list of rows is a place a strategy could write its own result.
        self._state.realized_pnl += realized - commission
        order.filled_size += fill.size
        if order.remaining == 0:
            order.status = OrderStatus.FILLED
            self._working.pop(order.order_id, None)

        self.n_fills += 1
        if len(self._fill_log) >= MAX_FILL_LOG:
            raise FillLogFull(
                f"more than {MAX_FILL_LOG:,} fills in one run. The log is what the "
                "score is computed from, so it is not truncated: fix the strategy "
                "or raise the bound deliberately."
            )
        self._fill_log.append(
            FillRecord(
                seq=tick.seq,
                ts=tick.ts,
                session=tick.session,
                order_id=order.order_id,
                side=order.side,
                size=fill.size,
                price=fill.price,
                # What the order did to the book, from its type: a market or a
                # triggered stop crossed; a resting limit was crossed into.
                liquidity=(
                    "maker"
                    if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
                    else "taker"
                ),
                tag=self._order_tags.get(order.order_id, ""),
            )
        )
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
        # Armed on the **first** fill and resized as more arrive, not on full
        # fill. A 10-lot entry filling 4+1+2+3 used to get a 3-lot stop and a
        # 3-lot target -- the last fragment -- leaving seven contracts naked for
        # the whole sequence, and the bracket did not exist at all until the
        # final fragment landed. Protected quantity now equals filled quantity at
        # every point, which is the property the bracket is for.
        if order.bracket is not None and order.filled_size > 0:
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
        existing = [
            o for o in self._book.working.values()
            if o.parent_id == parent.order_id
        ]
        if existing:
            # Already armed on an earlier fragment: widen the legs to the
            # cumulative filled quantity rather than adding a second bracket.
            # Prices stay where the first fill put them -- re-pricing on every
            # fragment would let the protection drift with the fill sequence,
            # which is a different strategy, not a bigger one.
            for leg in existing:
                leg.size = parent.filled_size
                leg.eligible_from_seq = tick.seq + 1
            return
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
                    size=parent.filled_size,
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
                    size=parent.filled_size,
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
        for other in list(self._working.values()):
            if (
                other.order_id != filled.order_id
                and other.oco_group == filled.oco_group
            ):
                self._cancel(other, LifecycleKind.OCO_CANCELLED, tick.ts, tick.seq)

    def _alloc_order_id(self) -> int:
        return self._book.allocate()

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
        self._working[oid] = order
        if s.tag:
            self._order_tags[oid] = s.tag
        self._emit(LifecycleKind.ORDER_ACCEPTED, tick.ts, tick.seq, oid)
        return order

    def _clamp_reduce_only(self, s: Submit, tick: Tick, oid: int) -> Submit | None:
        """A reduce-only order may not open or increase exposure.

        Accepted, carried on the order and checked nowhere until Task 7: a
        strategy passing reduce_only=True on an oversized order got a position
        flip and no complaint. A parameter that is accepted and silently ignored
        is worse than an absent one, because the strategy believes it took
        effect.
        """
        pos = self.position
        if pos == 0:
            self._emit(
                LifecycleKind.ORDER_REJECTED, tick.ts, tick.seq, oid,
                "reduce_only with no position open",
            )
            return None
        if (pos > 0) == (s.side is Side.BUY):
            self._emit(
                LifecycleKind.ORDER_REJECTED, tick.ts, tick.seq, oid,
                f"reduce_only {s.side} would increase a position of {pos}",
            )
            return None
        # Working reduce-only orders already spoken for -- a bracket's stop and
        # target are both reduce-only against the same position, and clamping
        # each to the full size is correct because only one of them can fill.
        if s.size > abs(pos):
            self._emit(
                LifecycleKind.ORDER_ACCEPTED, tick.ts, tick.seq, oid,
                f"reduce_only size clamped from {s.size} to {abs(pos)}",
            )
            return replace(s, size=abs(pos))
        return s

    def _cancel(self, order: Order, kind: LifecycleKind, ts: Ts, seq: int) -> None:
        order.status = (
            OrderStatus.EXPIRED
            if kind in (LifecycleKind.DAY_EXPIRED, LifecycleKind.GTD_EXPIRED)
            else OrderStatus.CANCELLED
        )
        self._working.pop(order.order_id, None)
        self._emit(kind, ts, seq, order.order_id)

    def _working_ids(self) -> tuple[int, ...]:
        return self._book.ids()

    # ---- callbacks and actions ---------------------------------------------- #
    def _check_budget(self) -> None:
        now = time.monotonic()
        if now > self._deadline:
            raise BudgetExceeded(
                self.wall_clock_budget_s,
                now - (self._deadline - self.wall_clock_budget_s),
                self.n_ticks,
            )

    def _dispatch(self, kind: str, event, tick: Tick, actionable: bool) -> None:
        """Build the callback's context, run the callback, apply what it queued.

        The context is expired the moment the callback returns: a strategy that
        stashes it and reaches for it later would be reading state from an
        instant it is not standing in.
        """
        queue = IntentQueue(self.action_cap, self._book.allocate, live=actionable)
        port = self._port
        common = dict(
            ts=tick.ts,
            seq=tick.seq,
            session=tick.session,
            fills=tuple(self._fills_since_callback),
            actionable=actionable,
        )
        if kind == "bars":
            ctx = BarContext(port, queue, event=event, **common)
            handler = self.strategy.on_bars
        elif kind == "tick":
            ctx = TickContext(port, queue, tick=event, **common)
            handler = self.strategy.on_tick
        elif kind == "fill":
            ctx = FillContext(port, queue, fill=event, **common)
            handler = self.strategy.on_fill
        else:
            ctx = LifecycleContext(port, queue, event=event, **common)
            handler = self.strategy.on_lifecycle

        self._callbacks += 1
        if self._deadline and self._callbacks % BUDGET_CHECK_EVERY_CALLBACKS == 0:
            self._check_budget()

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
                if a.reduce_only:
                    a = self._clamp_reduce_only(a, tick, oid)
                    if a is None:
                        continue
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
                    if a.size <= o.filled_size:
                        self._emit(
                            LifecycleKind.ORDER_REJECTED, tick.ts, tick.seq, a.order_id,
                            f"cannot resize to {a.size}: {o.filled_size} already filled",
                        )
                        continue
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

    # ---- the log, and what is derived from it ------------------------------ #
    @property
    def fill_log(self) -> tuple[FillRecord, ...]:
        """Every fill, frozen, in the order they happened."""
        return tuple(self._fill_log)

    def reconstructed(self) -> Blotter:
        """The blotter the fills imply, computed from the log and nothing else.

        This is what the scorer reads. `self.blotter` is the accumulator's
        version and is now a diagnostic: identical in every honest run, and the
        two disagreeing is a finding.
        """
        return reconstruct(
            self._fill_log, commission_per_contract=self.commission
        )

    def fill_log_digest(self) -> str:
        return log_digest(self._fill_log)

    def reconciliation(self) -> dict:
        """Accumulated against reconstructed.

        A bonus, not the protection. Anything that can reach the accumulator can
        reach both sides of this comparison, so it does not defend against a
        determined caller -- the defence is that the score is computed from a log
        nothing can reach. This catches the other thing: the two derivations
        drifting apart because one of them was changed and the other was not.
        """
        rebuilt = self.reconstructed()
        return {
            "accumulated_net_pnl": self.blotter.realized_pnl - self.blotter.commission,
            "reconstructed_net_pnl": rebuilt.realized_pnl - rebuilt.commission,
            "accumulated_digest": self.blotter.digest(),
            "reconstructed_digest": rebuilt.digest(),
            "fills": len(self._fill_log),
            "agree": rebuilt.digest() == self.blotter.digest(),
        }

    def _fit_now(self) -> None:
        """Call `fit()` once, at the edge of the warmup window, and never again.

        **The refusal is the point, not the call.** `nq_campaign.py --stage validate`
        carries a campaign winner to the validation split by writing its *source*
        and running it. For a strategy that is only a file that is complete; for a
        fitted one, a `fit()` that ran there would retrain **on the validation
        split**. Nothing would fail, nothing would be logged, and the resulting
        number would be flattering and meaningless -- on the one split that exists
        to be uncontaminated.

        So a second call raises. A run that needs weights it has not got must load
        them, and a missing weights file is an error rather than a licence to refit.
        """
        if self._has_fit:
            raise RuntimeError(
                "fit() has already been called for this run. A fitted strategy fits "
                "once, at the edge of its warmup window, on the data it accumulated "
                "there. Refitting mid-run means the trades before and after came "
                "from different models, and refitting on a later split means "
                "training on data the run is about to be scored against. Load "
                "weights instead. See docs/design/ml-strategies.md."
            )
        self._has_fit = True
        self.strategy.fit()
        self._fit_completed = True

    def _split_sessions(self) -> list[int]:
        """Every session of the served split, fit window included."""
        own = getattr(self.dataset, "scored_sessions", None)
        if own is not None:
            return list(own(self.split, self.warmup_sessions))

        from ..data.dataset import sessions_for

        _, live = sessions_for(self.dataset.calendar(), self.split,
                               self.warmup_sessions)
        return list(live)

    def _fit_set(self) -> set[int]:
        """The first `fit_sessions` of the split, cached.

        Computed from the split rather than from warmup, because warmup lives
        *before* the split and TRAIN -- where campaigns run -- has none.
        """
        if self._fit_cache is None:
            self._fit_cache = (set(self._split_sessions()[:self.fit_sessions])
                               if self.fit_sessions > 0 else set())
        return self._fit_cache

    def _is_actionable(self, session: int) -> bool:
        """Warmup is context from before the split; the fit window is a prefix of
        it. Both deliver bars and ignore actions, for different reasons."""
        return session not in self._warm and session not in self._fit_set()

    def scored_sessions(self) -> list[int]:
        """The actionable sessions of the served split, in the dataset's own
        session-id space -- the observation unit the metric scores on.

        Warmup sessions are excluded: the strategy saw their bars but could not
        act, so they are not observations. **The fit window is excluded for the
        same reason** -- a fitted strategy is required not to trade there, and
        leaving those sessions in the calendar would have Amendment 3's idle
        charge bill every one of them, making the score an artefact of how long
        the model trained for.

        A dataset that numbers its sessions by something other than position says
        so by defining `scored_sessions`. The NQ adapter does: `Tick.session`
        there is a Gregorian ordinal, deliberately, because a position means
        something different depending on how many sessions the spec trimmed to.
        Handing positions to `session_bounds()` would have produced dates in the
        year 2 AD and per-session returns keyed to sessions that do not exist --
        wrong in a way that nothing downstream could notice. The synthetic
        dataset numbers by position and needs no such method.
        """
        fit = self._fit_set()
        return [x for x in self._split_sessions() if x not in fit]

    def session_calendar(self):
        from ..scoring.metrics import session_calendar

        return session_calendar(self.dataset, self.scored_sessions())

    # ---- record -------------------------------------------------------------- #
    def _make_record(self, outcome: str, error: str | None, wall: float) -> RunRecord:
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
            ticks_stepped=self.n_stepped,
            tick_events_delivered=self.n_tick_events,
            bars_delivered=self.n_bars,
            fills=self.n_fills,
            outcome=outcome,
            error=error,
            warmup_sessions=self.warmup_sessions,
            wall_clock_budget_s=self.wall_clock_budget_s,
            end_of_session_positions=dict(self.end_of_session_positions),
            holdout_unlock_reason=self.dataset.holdout_unlock_reason,
            blotter_digest=self.blotter.digest(),
            fill_log_digest=self.fill_log_digest(),
            fills_logged=len(self._fill_log),
            reconciliation=self.reconciliation(),
        )
