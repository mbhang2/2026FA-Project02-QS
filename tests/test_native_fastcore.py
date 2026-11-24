"""The native market-order walk matches the Python fill model exactly."""

from __future__ import annotations

import numpy as np
import pytest

from src.core.data.okx_book import SessionBook, encode_depth
from src.core.engine import native
from src.core.engine.fills import BookQueueFill
from src.core.types import OrderType, Side, Tick

if not native.LIBRARY.exists():
    pytest.skip("the native library is not built", allow_module_level=True)

MS = 1_000_000


class Plain:
    order_id = 1
    order_type = OrderType.MARKET
    limit_price = None
    stop_armed = False

    def __init__(self, side, size, at):
        self.side, self.remaining, self.eligible_from_ts = side, size, at


def build_book(n=600, seed=4):
    rng = np.random.default_rng(seed)
    ts = np.cumsum(rng.integers(0, 3, n)) * MS + 10 * MS
    mid = 1_000_000 + np.cumsum(rng.integers(-2, 3, n))
    bid_px = np.column_stack([(mid - 1 - k) / 10 for k in range(5)])
    ask_px = np.column_stack([(mid + 1 + k) / 10 for k in range(5)])
    bid_lots = rng.integers(0, 40, (n, 5))
    ask_lots = rng.integers(0, 40, (n, 5))
    for side_px, side_lots in ((bid_px, bid_lots), (ask_px, ask_lots)):
        empty = rng.random((n, 5)) < 0.05
        side_px[empty], side_lots[empty] = np.nan, 0
    depth = {"scale": 10}
    for name, px, lots in (("bid", bid_px, bid_lots), ("ask", ask_px, ask_lots)):
        depth[f"{name}_px"], depth[f"{name}_lots"] = encode_depth(px[:, 1:], lots[:, 1:], 10)
    return SessionBook(ts.astype(np.int64), bid_px[:, 0].copy(), ask_px[:, 0].copy(),
                       bid_lots[:, 0].astype(np.int64), ask_lots[:, 0].astype(np.int64), depth=depth)


@pytest.mark.parametrize("two_sided", [False, True])
def test_every_order_matches_the_python_model(two_sided):
    book = build_book()
    rng = np.random.default_rng(9)
    n = 4_000
    at = book.ts[rng.integers(0, book.ts.size, n)] + rng.integers(-2, 3, n) * MS
    tick = at + rng.integers(0, 4, n) * MS
    side = rng.choice((-1, 1), n).astype(np.int8)
    size = rng.integers(1, 180, n).astype(np.int64)
    got = native.take_market_batch(native.load(), book, at, tick, side, size, require_two_sided=two_sided)
    model = BookQueueFill()
    model.bind_book(lambda session: book)
    model.require_two_sided = two_sided
    filled = rejected = 0
    for i in range(n):
        order = Plain(Side.BUY if side[i] > 0 else Side.SELL, int(size[i]), int(at[i]))
        fill = model.resolve(order, Tick(int(tick[i]), 100.0, 1, 1, side=Side.BUY))
        refused = model.take_rejection(1) is not None
        if fill is not None:
            filled += 1
            assert got["status"][i] == native.FILLED
            assert got["size"][i] == fill.size and got["price"][i] == fill.price
            assert got["mid"][i] == fill.mid or (np.isnan(got["mid"][i]) and np.isnan(fill.mid))
        elif refused:
            rejected += 1
            assert got["status"][i] == native.REJECTED
        else:
            assert got["status"][i] == native.WAIT
    assert filled > 500 and rejected > 100


def test_a_wrong_array_type_is_refused():
    book = build_book(20)
    with pytest.raises(TypeError):
        native.take_market_batch(native.load(), book, np.zeros(1), np.zeros(1, np.int64),
                                 np.ones(1, np.int8), np.ones(1, np.int64))
