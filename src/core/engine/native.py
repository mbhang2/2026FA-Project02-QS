"""The native event core, through a plain C interface. Not used by any backtest yet.

`BookQueueFill` remains the reference. A native piece joins the engine only after it has been
shown to reproduce that model exactly on real sessions.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import numpy as np

from .._roots import repo_root
from .fills import MAX_QUOTE_AGE_NS

LIBRARY = repo_root() / ".scratch" / "native" / "libfastcore.so"
ABI_VERSION = 2
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
    return lib


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
