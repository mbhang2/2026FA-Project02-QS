"""Amendment 23: no fill is priced from a book with an empty best side."""

from __future__ import annotations

import math

import numpy as np
import pytest

from src.core.data.okx_book import SessionBook
from src.core.engine.fills import BookQueueFill
from src.core.types import Order, OrderType, Side, Tick

MS = 1_000_000
NAN = float("nan")


def book():
    ts = np.array([10, 20, 30, 40], dtype=np.int64) * MS
    bid = np.array([100.0, NAN, 100.0, 100.0])
    ask = np.array([101.0, 102.0, 101.0, 101.0])
    qty = np.array([50, 0, 50, 50], dtype=np.int64)
    return SessionBook(ts, bid, ask, qty, np.full(4, 50, dtype=np.int64))


def model(on=True, session_book=None):
    m = BookQueueFill()
    m.bind_book(lambda session: session_book or book())
    m.require_two_sided = on
    return m


def order(side=Side.BUY, kind=OrderType.MARKET, limit=None, eligible=None):
    o = Order.__new__(Order)
    for name, value in (("order_id", 1), ("side", side), ("order_type", kind), ("limit_price", limit),
                        ("stop_price", None), ("size", 1), ("filled", 0), ("eligible_from_ts", eligible),
                        ("stop_armed", False)):
        try:
            setattr(o, name, value)
        except AttributeError:
            pass
    return o


def tick(ms, price=100.5, side=Side.BUY):
    return Tick(ms * MS, price, 1, 1, side=side)


class Plain:
    def __init__(self, side=Side.BUY, kind=OrderType.MARKET, limit=None, eligible=None):
        self.order_id, self.side, self.order_type, self.limit_price = 1, side, kind, limit
        self.eligible_from_ts, self.remaining, self.stop_armed = eligible, 1, False


def test_without_the_rule_a_one_sided_row_gives_an_undefined_mid():
    fill = model(False).resolve(Plain(eligible=20 * MS), tick(21))
    assert fill is not None and math.isnan(fill.mid)


def test_a_taker_waits_for_a_two_sided_book():
    m = model()
    o = Plain(eligible=20 * MS)
    assert m.resolve(o, tick(21)) is None
    fill = m.resolve(o, tick(31))
    assert fill is not None and fill.price == 101.0 and fill.mid == 100.5


def test_a_limit_is_not_placed_on_a_one_sided_row():
    m = model()
    o = Plain(kind=OrderType.LIMIT, limit=99.0, eligible=20 * MS)
    assert m.resolve(o, tick(21)) is None
    assert o.order_id not in m._resting
    m.resolve(o, tick(31))
    assert o.order_id in m._resting


def test_a_resting_limit_cannot_fill_while_one_sided():
    ts = np.array([10, 20], dtype=np.int64) * MS
    b = SessionBook(ts, np.array([100.0, 100.0]), np.array([101.0, NAN]),
                    np.array([5, 5], dtype=np.int64), np.array([5, 0], dtype=np.int64))
    m = model(session_book=b)
    o = Plain(kind=OrderType.LIMIT, limit=100.0, eligible=10 * MS)
    assert m.resolve(o, tick(11, price=100.5)) is None
    assert m.resolve(o, tick(21, price=99.0, side=Side.SELL)) is None


def test_the_forced_close_uses_the_last_two_sided_row():
    ts = np.array([10, 20], dtype=np.int64) * MS
    b = SessionBook(ts, np.array([100.0, NAN]), np.array([101.0, 102.0]),
                    np.array([5, 0], dtype=np.int64), np.array([5, 5], dtype=np.int64))
    fill = model(session_book=b).settle(Side.BUY, 1, 1, 99.0)
    assert fill.price == 101.0 and fill.mid == 100.5


def test_two_sided_fills_are_unchanged():
    a, b = model(False), model(True)
    assert a.resolve(Plain(eligible=40 * MS), tick(41)) == b.resolve(Plain(eligible=40 * MS), tick(41))
