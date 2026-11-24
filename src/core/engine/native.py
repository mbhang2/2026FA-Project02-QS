"""The native event core, through a plain C interface. Not used by any backtest yet.

`BookQueueFill` remains the reference. A native piece joins the engine only after it has been
shown to reproduce that model exactly on real sessions.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import numpy as np

from .._roots import repo_root
from ..types import DEFAULT_ASSET, FillRecord, Side
from .fills import MAX_QUOTE_AGE_NS
from .reconstruct import log_digest, reconstruct

LIBRARY = repo_root() / ".scratch" / "native" / "libfastcore.so"
ABI_VERSION = 8
WAIT, FILLED, REJECTED = 0, 1, 2


def load(path: Path = LIBRARY) -> ctypes.CDLL:
    lib = ctypes.CDLL(str(path))
    lib.fc_abi_version.restype = ctypes.c_int32
    if lib.fc_abi_version() != ABI_VERSION:
        raise RuntimeError(f"{path} has ABI {lib.fc_abi_version()}, expected {ABI_VERSION}")
    lib.fc_take_market_batch.restype = ctypes.c_int64
    pointer = ctypes.c_void_p
    lib.fc_take_market_batch.argtypes = [
        *[pointer] * 9, ctypes.c_int64, ctypes.c_int32, ctypes.c_int32, ctypes.c_int64,
        *[pointer] * 4, ctypes.c_int64, *[pointer] * 5,
    ]
    lib.fc_run_limit_orders.restype = ctypes.c_int64
    lib.fc_run_limit_orders.argtypes = [
        *[pointer] * 9, ctypes.c_int64, ctypes.c_int32, ctypes.c_int32, ctypes.c_int64,
        *[pointer] * 10, ctypes.c_int64, ctypes.c_int64, *[pointer] * 6,
    ]
    lib.fc_engine_new.restype = pointer
    lib.fc_engine_new.argtypes = [ctypes.c_int64, ctypes.c_int64, ctypes.c_int32, ctypes.c_int32,
                                  ctypes.c_int32, ctypes.c_int32, ctypes.c_int32, ctypes.c_int64]
    lib.fc_engine_free.argtypes = [pointer]
    lib.fc_engine_free.restype = None
    lib.fc_engine_run_session.restype = ctypes.c_int64
    lib.fc_engine_run_session.argtypes = [
        pointer, *[pointer] * 9, ctypes.c_int64, *[pointer] * 4, ctypes.c_int64, ctypes.c_int64,
        ctypes.c_int64, ctypes.c_int32, pointer, pointer, ctypes.c_int64, pointer, pointer, ctypes.c_int32,
    ]
    lib.fc_engine_snapshot.restype = ctypes.c_int64
    lib.fc_engine_snapshot.argtypes = [pointer, pointer, pointer]
    lib.fc_engine_snapshot_orders.restype = None
    lib.fc_engine_snapshot_orders.argtypes = [pointer, pointer, pointer]
    for name in ("fc_engine_fill_count", "fc_engine_position", "fc_engine_callbacks", "fc_engine_seq"):
        getattr(lib, name).restype = ctypes.c_int64
        getattr(lib, name).argtypes = [pointer]
    lib.fc_engine_copy_fills.restype = None
    lib.fc_engine_copy_fills.argtypes = [pointer, *[pointer] * 9]
    return lib


class ParityState(ctypes.Structure):
    """The fixed strategy's parameters and call counter."""

    _fields_ = [("every", ctypes.c_int64), ("hold", ctypes.c_int64), ("offset", ctypes.c_int64),
                ("size", ctypes.c_int64), ("market_every", ctypes.c_int64), ("count", ctypes.c_int64)]


class NativeEngine:
    """One run of the native event loop for one asset."""

    def __init__(self, lib: ctypes.CDLL, *, ticket, latency_ns: int, scale: int = 10,
                 require_two_sided: bool = True, action_cap: int = 64, book_features: bool = False,
                 book_events: bool = False, inventory_cap: int = 0):
        from ..runlog import RunNotAccounted, RunTicket

        # Like `Engine`: a native run is accounted for, through a trial's child or a test.
        if not isinstance(ticket, RunTicket) or ticket.kind not in ("native", "test"):
            raise RunNotAccounted("NativeEngine needs RunTicket.for_native_child or RunTicket.for_test")
        self.ticket = ticket
        self.lib = lib
        self.scale = scale
        self.handle = lib.fc_engine_new(latency_ns, MAX_QUOTE_AGE_NS, scale,
                                        int(bool(require_two_sided)), action_cap, int(bool(book_features)),
                                        int(bool(book_events)), int(inventory_cap))

    def close(self) -> None:
        if self.handle:
            self.lib.fc_engine_free(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def run_session(self, book, trades: tuple, session: int, close_ts: int, *, actionable: bool = True,
                    callback=None, user=None, snapshot_trade: int = -1, state=None) -> None:
        """`snapshot_trade` saves the engine, and a copy of `state`, just before that trade."""
        state_src = None if state is None else _array(state, np.float64)
        self._snap_state = None if state_src is None else np.zeros_like(state_src)
        depth = book._depth
        if depth is None:
            raise ValueError("the native engine needs the stored depth columns")
        if int(depth["scale"]) != self.scale:
            raise ValueError("this book's price scale differs from the engine's")
        columns = [_array(book.ts, np.int64), _array(book.bid, np.float64), _array(book.ask, np.float64),
                   _array(book.bid_qty, np.int64), _array(book.ask_qty, np.int64)]
        levels = [_array(depth[name], np.int32, 2)
                  for name in ("bid_px", "bid_lots", "ask_px", "ask_lots")]
        ts, price, size, side = (_array(trades[0], np.int64), _array(trades[1], np.float64),
                                 _array(trades[2], np.int64), _array(trades[3], np.int8))
        if not (ts.size == price.size == size.size == side.size):
            raise ValueError("trade columns must have equal lengths")
        result = self.lib.fc_engine_run_session(
            self.handle, *[a.ctypes.data for a in (*columns, *levels)], columns[0].size,
            ts.ctypes.data, price.ctypes.data, size.ctypes.data, side.ctypes.data, ts.size,
            session, close_ts, int(bool(actionable)), callback, user, snapshot_trade,
            None if state_src is None else state_src.ctypes.data,
            None if self._snap_state is None else self._snap_state.ctypes.data,
            0 if state_src is None else state_src.size)
        if result < 0:
            raise RuntimeError("the native strategy callback stopped the run")

    def snapshot(self) -> dict | None:
        """The engine saved by the last `run_session`'s `snapshot_trade`, or None."""
        scalars, price = np.zeros(5, np.int64), np.zeros(1, np.float64)
        n = int(self.lib.fc_engine_snapshot(self.handle, scalars.ctypes.data, price.ctypes.data))
        if not scalars[0]:
            return None
        ints, doubles = np.zeros((n, 14), np.int64), np.zeros((n, 2), np.float64)
        self.lib.fc_engine_snapshot_orders(self.handle, ints.ctypes.data, doubles.ctypes.data)
        return {"seq": int(scalars[1]), "position": int(scalars[2]), "next_id": int(scalars[3]),
                "trade": int(scalars[4]), "last_price": float(price[0]), "order_ints": ints,
                "order_doubles": doubles, "state": self._snap_state}

    @property
    def seq(self) -> int:
        """The last event's sequence number, -1 before the first."""
        return int(self.lib.fc_engine_seq(self.handle))

    @property
    def position(self) -> int:
        return int(self.lib.fc_engine_position(self.handle))

    def fill_columns(self) -> dict:
        n = int(self.lib.fc_engine_fill_count(self.handle))
        seq, ts, session, oid, size = (np.empty(n, np.int64) for _ in range(5))
        side, maker = np.empty(n, np.int8), np.empty(n, np.int8)
        price, mid = np.empty(n, np.float64), np.empty(n, np.float64)
        self.lib.fc_engine_copy_fills(self.handle, *[a.ctypes.data for a in (
            seq, ts, session, oid, side, size, price, maker, mid)])
        return {"seq": seq, "ts": ts, "session": session, "order_id": oid, "side": side, "size": size,
                "price": price, "maker": maker, "mid": mid}

    def fill_records(self) -> list[FillRecord]:
        c = self.fill_columns()
        seq, ts, session, oid, side, size, price, maker, mid = (
            c["seq"], c["ts"], c["session"], c["order_id"], c["side"], c["size"], c["price"], c["maker"], c["mid"])
        n = seq.size
        return [FillRecord(int(seq[i]), int(ts[i]), int(session[i]), int(oid[i]),
                           Side.BUY if side[i] > 0 else Side.SELL, int(size[i]), float(price[i]),
                           "maker" if maker[i] else "taker", "", DEFAULT_ASSET, float(mid[i]))
                for i in range(n)]

    def fill_digest(self) -> str:
        return log_digest(self.fill_records())

    def reconstructed(self, **costs):
        """The blotter these fills imply, through the same function the Python engine uses."""
        return reconstruct(self.fill_records(), **costs)

    def scored(self, *, costs: dict, period, terms: dict, min_exposed_fraction=None):
        """(blotter, score) through the unchanged scorer. Writes no ledger row."""
        from ..scoring.scorer import score

        blotter = self.reconstructed(**costs)
        return blotter, score(blotter, period=period, notional_base=terms["notional_base"],
                              idle_charge=terms["idle_charge"], funding=terms["funding"],
                              min_exposed_fraction=min_exposed_fraction)


def _array(values, dtype, ndim: int = 1) -> np.ndarray:
    out = np.asarray(values)
    if out.dtype != np.dtype(dtype) or out.ndim != ndim or not out.flags.c_contiguous:
        raise TypeError(f"expected a contiguous {ndim}-d {dtype} array, got {out.dtype} {out.shape}")
    return out


def run_limit_orders(lib: ctypes.CDLL, book, trades: tuple, side, limit, size, eligible, start, end,
                     *, require_two_sided: bool = True, capacity: int | None = None) -> dict:
    """Limit orders through a window of trades, with queue position kept in native code."""
    depth = book._depth
    if depth is None:
        raise ValueError("the native walk needs the stored depth columns")
    columns = [_array(book.ts, np.int64), _array(book.bid, np.float64), _array(book.ask, np.float64),
               _array(book.bid_qty, np.int64), _array(book.ask_qty, np.int64)]
    levels = [_array(depth[name], np.int32, 2)
              for name in ("bid_px", "bid_lots", "ask_px", "ask_lots")]
    rows = columns[0].size
    if any(a.shape != (rows, 4) for a in levels):
        raise ValueError("depth columns must be rows x 4")
    trade_ts, trade_px, trade_size, trade_side = (
        _array(trades[0], np.int64), _array(trades[1], np.float64),
        _array(trades[2], np.int64), _array(trades[3], np.int8))
    if not (trade_ts.size == trade_px.size == trade_size.size == trade_side.size):
        raise ValueError("trade columns must have equal lengths")
    sides, limits, sizes, when, first, last = (
        _array(side, np.int8), _array(limit, np.float64), _array(size, np.int64),
        _array(eligible, np.int64), _array(start, np.int64), _array(end, np.int64))
    n = sides.size
    if any(a.size != n for a in (limits, sizes, when, first, last)) or (sizes <= 0).any():
        raise ValueError("orders must have equal lengths and positive sizes")
    if n and (first.min() < 0 or last.max() > trade_ts.size or (first > last).any()):
        raise ValueError("an order's trade window is outside the session")
    room = int(capacity if capacity is not None else max(1, int(sizes.sum())))
    out_order, out_trade, out_size = (np.empty(room, np.int64) for _ in range(3))
    out_price, out_mid = np.empty(room, np.float64), np.empty(room, np.float64)
    out_maker = np.empty(room, np.int8)
    count = lib.fc_run_limit_orders(
        *[a.ctypes.data for a in (*columns, *levels)], rows, int(depth["scale"]),
        int(bool(require_two_sided)), MAX_QUOTE_AGE_NS,
        *[a.ctypes.data for a in (trade_ts, trade_px, trade_size, trade_side,
                                  sides, limits, sizes, when, first, last)],
        n, room, *[a.ctypes.data for a in (out_order, out_trade, out_size, out_price, out_mid, out_maker)])
    if count < 0:
        raise RuntimeError("the fill output capacity was too small")
    return {"order": out_order[:count], "trade": out_trade[:count], "size": out_size[:count],
            "price": out_price[:count], "mid": out_mid[:count], "maker": out_maker[:count]}


def take_market_batch(lib: ctypes.CDLL, book, order_at, tick_ts, side, size,
                      *, require_two_sided: bool = True) -> dict:
    """Market orders against one session's stored book, without touching Python per order."""
    depth = book._depth
    if depth is None:
        raise ValueError("the native walk needs the stored depth columns")
    columns = [_array(book.ts, np.int64), _array(book.bid, np.float64), _array(book.ask, np.float64),
               _array(book.bid_qty, np.int64), _array(book.ask_qty, np.int64)]
    rows = columns[0].size
    levels = [_array(depth[name], np.int32, 2)
              for name in ("bid_px", "bid_lots", "ask_px", "ask_lots")]
    if any(a.shape != (rows, 4) for a in levels):
        raise ValueError("depth columns must be rows x 4")
    at, tick, sides, sizes = (_array(order_at, np.int64), _array(tick_ts, np.int64),
                              _array(side, np.int8), _array(size, np.int64))
    n = at.size
    if not (tick.size == sides.size == sizes.size == n) or (sizes <= 0).any():
        raise ValueError("orders must have equal lengths and positive sizes")
    status, fill_size = np.empty(n, np.int8), np.empty(n, np.int64)
    price, mid, row = np.empty(n, np.float64), np.empty(n, np.float64), np.empty(n, np.int64)
    pointers = [a.ctypes.data for a in (*columns, *levels)]
    filled = lib.fc_take_market_batch(
        *pointers, rows, int(depth["scale"]), int(bool(require_two_sided)), MAX_QUOTE_AGE_NS,
        at.ctypes.data, tick.ctypes.data, sides.ctypes.data, sizes.ctypes.data, n,
        status.ctypes.data, fill_size.ctypes.data, price.ctypes.data, mid.ctypes.data, row.ctypes.data)
    return {"filled": int(filled), "status": status, "size": fill_size, "price": price, "mid": mid, "row": row}
