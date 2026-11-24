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
ABI_VERSION = 1
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
    return lib


def _array(values, dtype, ndim: int = 1) -> np.ndarray:
    out = np.asarray(values)
    if out.dtype != np.dtype(dtype) or out.ndim != ndim or not out.flags.c_contiguous:
        raise TypeError(f"expected a contiguous {ndim}-d {dtype} array, got {out.dtype} {out.shape}")
    return out


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
