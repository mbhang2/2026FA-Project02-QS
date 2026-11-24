"""The checks a native kernel trial must pass. Nothing here runs a trial or writes a ledger row.

The score is computed only from fills, so `validate_columns` refuses anything a sound run could
not have produced. `KernelStrategy` drives the same compiled kernel through the Python engine for
an audit. How a candidate kernel gets a ticket, and so how it counts toward N, is left to an
amendment: a calibration ticket is reserved for the committed reference strategy.
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


def refusal(profile: dict) -> str | None:
    """Why this profile cannot run natively, or None."""
    if Split(profile["split"]) is not Split.TRAIN:
        return "the native check runs on the train split only"
    if profile.get("fill_model") != BOOK_MODEL:
        return f"the native engine implements only {BOOK_MODEL}"
    if not profile.get("book_features"):
        return "the native check needs book_features: true"
    if profile.get("warmup_sessions", 0) or profile.get("fit_sessions", 0) or profile.get("weights_path"):
        return "warmup and fitted strategies are not ported"
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


class KernelStrategy:
    """The same compiled kernel, driven by the Python engine for the audit."""

    name = "check/kernel-audit"

    def __init__(self, source: str, *, capacity: int = 64):
        self.kernel = K.CompiledKernel(source)
        self.capacity = capacity
        self.event, self.book = np.zeros(41, np.int64), np.zeros(24)
        self.orders = np.zeros((capacity, 6))
        self.last_price = 0.0
        call = self.kernel._entry.ctypes
        self._call = call
        self._pointers = (self.event.ctypes.data_as(call.argtypes[0]),
                          self.book.ctypes.data_as(call.argtypes[1]),
                          self.kernel.state.ctypes.data_as(call.argtypes[2]),
                          self.orders.ctypes.data_as(call.argtypes[3]))

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
        side = 1 if tick.side is Side.BUY else -1 if tick.side is Side.SELL else 0
        self._run(ctx, tick.price, tick.size, side, 0)

    def on_book_event(self, ctx):
        self._run(ctx, self.last_price, 0, 0, 1)

    def _run(self, ctx, price, size, side, kind):
        event, book, orders = self.event, self.book, self.orders
        event[:] = 0
        book[:] = 0.0
        orders[:] = 0.0
        working = ctx.working_order_ids[:32]
        event[:8] = (ctx.ts, ctx._seq, ctx.session, size, ctx.position, side, int(ctx.actionable), len(working))
        if working:
            event[8:8 + len(working)] = working
        event[K.CONSTANTS["E_KIND"]] = kind
        book[0] = price
        view = ctx.book()
        if view is not None:
            book[1:4] = (1.0, len(view.bid_prices), len(view.ask_prices))
            for offset, values in ((4, view.bid_prices), (9, view.bid_sizes), (14, view.ask_prices),
                                   (19, view.ask_sizes)):
                book[offset:offset + len(values)] = values
        count = self._call(*self._pointers, self.kernel.state.size, self.capacity)
        if count < 0 or count > self.capacity:
            raise NativeTrialFailed("the kernel stopped the audit run")
        for i in range(count):
            self._apply(ctx, orders[i], working)

    @staticmethod
    def _apply(ctx, row, working):
        op, kind, tif, size, limit, order_id = (float(x) for x in row)
        if op in (1.0, 2.0):
            valid = kind in (0.0, 1.0) and tif in (0.0, 1.0) and np.isfinite(size) and 1.0 <= size <= 2.0 ** 53 \
                and np.floor(size) == size and (kind == 0.0 or (np.isfinite(limit) and limit > 0.0))
            if not valid:
                raise NativeTrialFailed("the kernel returned an invalid order")
            place = ctx.buy if op == 1.0 else ctx.sell
            when = "GTC" if tif == 1.0 else "DAY"
            if kind == 0.0:
                place(int(size), tif=when)
            else:
                place(int(size), type="limit", limit=limit, tif=when)
        elif op == 3.0:
            if not any(float(w) == order_id for w in working):
                raise NativeTrialFailed("the kernel cancelled an order it cannot see")
            ctx.cancel(int(order_id))
        else:
            raise NativeTrialFailed("the kernel returned an invalid action")
