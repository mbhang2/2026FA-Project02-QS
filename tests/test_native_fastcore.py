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


class Limit(Plain):
    order_type = OrderType.LIMIT

    def __init__(self, order_id, side, size, at, limit):
        super().__init__(side, size, at)
        self.order_id, self.limit_price = order_id, limit


def python_limits(book, trades, side, limit, size, eligible, start, end, two_sided):
    model = BookQueueFill()
    model.bind_book(lambda session: book)
    model.require_two_sided = two_sided
    out = []
    for i in range(side.size):
        order = Limit(i + 1, Side.BUY if side[i] > 0 else Side.SELL, int(size[i]), int(eligible[i]), float(limit[i]))
        for t in range(int(start[i]), int(end[i])):
            code = int(trades[3][t])
            tick = Tick(int(trades[0][t]), float(trades[1][t]), int(trades[2][t]), 1,
                        side=Side.BUY if code > 0 else Side.SELL if code < 0 else None)
            fill = model.resolve(order, tick)
            if fill is not None:
                out.append((i, t, fill.size, fill.price, fill.mid, fill.liquidity == "maker"))
                order.remaining -= fill.size
                if order.remaining <= 0:
                    break
    return out


@pytest.mark.parametrize("two_sided", [False, True])
def test_limit_orders_match_tick_by_tick(two_sided):
    book = build_book(800, seed=11)
    rng = np.random.default_rng(12)
    n_trades = 900
    idx = np.sort(rng.integers(0, book.ts.size, n_trades))
    ts = np.asarray(book.ts)[idx] + rng.integers(0, 2, n_trades) * MS
    ts = np.maximum.accumulate(ts)
    reference = np.nan_to_num(np.asarray(book.bid)[idx], nan=100000.0)
    price = np.round(reference + rng.integers(-3, 5, n_trades) / 10, 1)
    trades = (ts.astype(np.int64), price.astype(float), rng.integers(1, 30, n_trades).astype(np.int64),
              rng.choice((-1, 0, 1), n_trades, p=(0.48, 0.04, 0.48)).astype(np.int8))
    m = 500
    start = rng.integers(0, n_trades - 2, m).astype(np.int64)
    end = np.minimum(start + rng.integers(1, 120, m), n_trades).astype(np.int64)
    side = rng.choice((-1, 1), m).astype(np.int8)
    limit = np.round(price[start] + rng.integers(-4, 5, m) / 10, 1)
    size = rng.integers(1, 70, m).astype(np.int64)
    eligible = ts[start] + rng.integers(0, 2, m) * MS
    got = native.run_limit_orders(native.load(), book, trades, side, limit, size, eligible.astype(np.int64),
                                  start, end, require_two_sided=two_sided)
    want = python_limits(book, trades, side, limit, size, eligible, start, end, two_sided)
    assert got["order"].size == len(want) and len(want) > 100
    assert set(got["maker"].tolist()) == {0, 1}
    for j, (i, t, fill_size, fill_price, mid, maker) in enumerate(want):
        assert (got["order"][j], got["trade"][j], got["size"][j], got["price"][j], bool(got["maker"][j])) == (
            i, t, fill_size, fill_price, maker)
        if mid is None or np.isnan(mid):
            assert np.isnan(got["mid"][j])
        else:
            assert got["mid"][j] == mid
