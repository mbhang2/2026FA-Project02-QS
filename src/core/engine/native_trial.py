"""The checks a native kernel trial must pass. Nothing here runs a trial or writes a ledger row.

The score is computed only from fills, so `validate_columns` refuses anything a sound run could
not have produced. `audit_strategy_source` turns a kernel into an ordinary strategy file for the
process boundary. `scripts/run_native_trial.py` owns the ledger row, under Amendment 24.
"""

from __future__ import annotations

import numpy as np

from ..types import Side, Split
from . import kernel as K

COLUMN_TYPES = {"seq": np.int64, "ts": np.int64, "session": np.int64, "order_id": np.int64,
                "side": np.int8, "size": np.int64, "price": np.float64, "maker": np.int8,
                "mid": np.float64}
BOOK_MODEL = "book-queue-pessimistic"


class NativeTrialRefused(ValueError):
    """The profile or source is outside what the native path has proved."""


class NativeTrialFailed(RuntimeError):
    """A child failed, returned invalid fills, or disagreed with the audit."""


class AuditMismatch(NativeTrialFailed):
    """The native fills differ from the Python replay. The trial is invalid and still counts."""


def refusal(profile: dict) -> str | None:
    """Why this profile cannot run natively, or None."""
    if Split(profile["split"]) is not Split.TRAIN:
        return "the native check runs on the train split only"
    if profile.get("fill_model") != BOOK_MODEL:
        return f"the native engine implements only {BOOK_MODEL}"
    if not profile.get("book_features"):
        return "the native check needs book_features: true"
    if profile.get("fit_sessions", 0) or profile.get("weights_path"):
        return "fitted strategies are not ported"
    if profile.get("action_cap", 64) != 64:
        return "the native engine and its audit are proved only at an action cap of 64"
    if not profile.get("allow_overnight", True):
        return "the overnight flatten is not ported"
    return None


def validate_columns(columns: dict, sessions: set[int], bounds) -> dict:
    """Refuse anything a sound run could not have produced."""
    if set(columns) != set(COLUMN_TYPES):
        raise NativeTrialFailed("the child returned the wrong fill columns")
    n = columns["seq"].size
    for name, dtype in COLUMN_TYPES.items():
        a = columns[name]
        if a.dtype != np.dtype(dtype) or a.ndim != 1 or a.size != n:
            raise NativeTrialFailed(f"fill column {name} has the wrong type or length")
    if not n:
        return columns
    checks = {
        "sequence": (np.diff(columns["seq"]) >= 0).all() and columns["seq"][0] >= 0,
        "order id": (columns["order_id"] > 0).all(),
        "side": np.isin(columns["side"], (-1, 1)).all(),
        "size": (columns["size"] > 0).all(),
        "maker flag": np.isin(columns["maker"], (0, 1)).all(),
        "price": np.isfinite(columns["price"]).all() and (columns["price"] > 0).all(),
        "mid": np.isfinite(columns["mid"]).all() and (columns["mid"] > 0).all(),
    }
    for name, good in checks.items():
        if not good:
            raise NativeTrialFailed(f"the child returned an invalid fill {name}")
    for session in np.unique(columns["session"]).tolist():
        if session not in sessions:
            raise NativeTrialFailed(f"the child returned a fill from session {session}")
        low, high = bounds(session)
        ts = columns["ts"][columns["session"] == session]
        if ts.min() < low or ts.max() > high:
            raise NativeTrialFailed(f"a fill falls outside session {session}")
    return columns


def records_from_columns(columns: dict) -> list:
    from ..types import DEFAULT_ASSET, FillRecord

    c = columns
    return [FillRecord(int(c["seq"][i]), int(c["ts"][i]), int(c["session"][i]), int(c["order_id"][i]),
                       Side.BUY if c["side"][i] > 0 else Side.SELL, int(c["size"][i]), float(c["price"][i]),
                       "maker" if c["maker"][i] else "taker", "", DEFAULT_ASSET, float(c["mid"][i]))
            for i in range(c["seq"].size)]


# The only kernel the runner accepts until Amendment 24 is signed. It is a fixed engine check.
CHECK_KERNEL = """
def kernel(event, book, state, orders):
    state[0] += 1.0
    if book[B_OK] == 0.0:
        state[2] += 1.0
        return 0
    for i in range(5):
        if i < book[B_N_BID]:
            state[1] = (state[1] * 31.0 + book[B_BID_PX + i] * book[B_BID_QTY + i]) % 1000000007.0
    for i in range(5):
        if i < book[B_N_ASK]:
            state[1] = (state[1] * 31.0 + book[B_ASK_PX + i] * book[B_ASK_QTY + i]) % 1000000007.0
    if state[0] % 50.0 != 0.0:
        return 0
    step = int(state[0]) // 50
    if event[E_N_WORKING] > 0 and step % 7 == 0:
        orders[0, O_OP] = OP_CANCEL
        orders[0, O_ORDER_ID] = event[E_WORKING]
        return 1
    if event[E_N_WORKING] >= 4:
        return 0
    buy = event[E_POSITION] < 0 or (event[E_POSITION] == 0 and step % 2 == 0)
    orders[0, O_OP] = OP_BUY if buy else OP_SELL
    orders[0, O_SIZE] = 20.0
    orders[0, O_TIF] = TIF_GTC if step % 3 == 0 else TIF_DAY
    if step % 5 == 0:
        orders[0, O_KIND] = KIND_MARKET
    else:
        orders[0, O_KIND] = KIND_LIMIT
        orders[0, O_LIMIT] = book[B_BID_PX] if buy else book[B_ASK_PX]
    return 1
"""

_AUDIT_TEMPLATE = """import numpy as np

{constants}

{kernel}


class Strategy:
    name = "check/kernel-audit"

    def __init__(self):
        self.state = np.array({state!r}, dtype=np.uint64).view(np.float64)
        self.last_price = np.array([{last_price!r}], dtype=np.uint64).view(np.float64)[0]

    def setup(self, ctx):
        ctx.subscribe("tick")
        ctx.subscribe("book")

    def on_bars(self, ctx):
        pass

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass

    def on_tick(self, ctx):
        tick = ctx.tick
        self.last_price = tick.price
        side = 0 if tick.side is None else int(tick.side.sign)
        self._run(ctx, tick.price, tick.size, side, EVENT_TRADE)

    def on_book_event(self, ctx):
        self._run(ctx, self.last_price, 0, 0, EVENT_BOOK)

    def _run(self, ctx, price, size, side, kind):
        event, values, orders = np.zeros(41, np.int64), np.zeros(24), np.zeros((64, 6))
        working = tuple(ctx.working_order_ids)[:32]
        event[E_TS], event[E_SESSION], event[E_SIZE] = ctx.ts, ctx.session, size
        event[E_POSITION], event[E_SIDE] = ctx.position, side
        event[E_ACTIONABLE], event[E_N_WORKING], event[E_KIND] = int(ctx.actionable), len(working), kind
        if working:
            event[E_WORKING:E_WORKING + len(working)] = working
        values[B_PRICE] = price
        view = ctx.book()
        if view is not None:
            values[B_OK], values[B_N_BID], values[B_N_ASK] = 1.0, len(view.bid_prices), len(view.ask_prices)
            for offset, levels in ((B_BID_PX, view.bid_prices), (B_BID_QTY, view.bid_sizes),
                                   (B_ASK_PX, view.ask_prices), (B_ASK_QTY, view.ask_sizes)):
                values[offset:offset + len(levels)] = levels
        count = int(kernel(event, values, self.state, orders))
        if count < 0 or count > 64:
            raise ValueError("the kernel returned an invalid action count")
        for i in range(count):
            self._apply(ctx, orders[i], working)

    @staticmethod
    def _apply(ctx, row, working):
        op, kind, tif, size, limit, order_id = (float(x) for x in row)
        if op in (OP_BUY, OP_SELL):
            good = kind in (KIND_MARKET, KIND_LIMIT) and tif in (TIF_DAY, TIF_GTC) and np.isfinite(size) \\
                and 1.0 <= size <= 2.0 ** 53 and np.floor(size) == size \\
                and (kind == KIND_MARKET or (np.isfinite(limit) and limit > 0.0))
            if not good:
                raise ValueError("the kernel returned an invalid order")
            place = ctx.buy if op == OP_BUY else ctx.sell
            when = "GTC" if tif == TIF_GTC else "DAY"
            if kind == KIND_MARKET:
                place(int(size), tif=when)
            else:
                place(int(size), type="limit", limit=limit, tif=when)
        elif op == OP_CANCEL:
            if not any(float(w) == order_id for w in working):
                raise ValueError("the kernel cancelled an order it cannot see")
            ctx.cancel(int(order_id))
        else:
            raise ValueError("the kernel returned an invalid action")
"""


def audit_strategy_source(source: str, state, last_price: float = 0.0) -> str:
    """The kernel as an ordinary strategy file, for the process boundary.

    Python executes the kernel source here, so the audit does not trust Numba's compiled code.
    `state` is the kernel's buffer at the end of warmup, handed over as its exact bits.
    """
    K.check(source)
    constants = "\n".join(f"{name} = {value}" for name, value in K.CONSTANTS.items())
    return _AUDIT_TEMPLATE.format(constants=constants, kernel=source.strip(),
                                  state=np.asarray(state, np.float64).view(np.uint64).tolist(),
                                  last_price=int(np.asarray([last_price], np.float64).view(np.uint64)[0]))


ORDER_FIELDS = ("id", "buy", "market", "gtc", "size", "filled", "eligible_seq", "eligible_ts", "cancelling",
                "cancel_at", "placed", "session", "row", "has_queue")


def resume_from_snapshot(snapshot: dict, after_ts: int) -> dict:
    """A native snapshot as the Python engine's `resume`, with sequence numbers made relative
    to the resumed run, whose first event is the native engine's `seq + 1`."""
    first = snapshot["seq"] + 1
    orders = []
    for ints, doubles in zip(snapshot["order_ints"].tolist(), snapshot["order_doubles"].tolist()):
        order = dict(zip(ORDER_FIELDS, ints))
        order["eligible_seq"] -= first
        order["limit"], order["queue"] = doubles
        orders.append(order)
    return {"position": snapshot["position"], "next_id": snapshot["next_id"], "last_price": snapshot["last_price"],
            "orders": orders, "after_ts": int(after_ts), "first_seq": first}


def trades_for_events(trade_ts: np.ndarray, book_ts: np.ndarray, limit: int, after: int | None = None) -> int:
    """How many leading trades cover at least `limit` events, trades and book events together.

    Book events count only after `after`, which is the session's first trade unless the run
    resumes mid-session, where it is the trade before the first one here.
    """
    releases = np.unique(book_ts - book_ts % 1_000_000) + 1_000_000
    releases = releases[releases > (trade_ts[0] if after is None else after)]
    events = np.arange(1, trade_ts.size + 1) + np.searchsorted(releases, trade_ts, side="right")
    return min(int(np.searchsorted(events, limit, side="left")) + 1, trade_ts.size)
