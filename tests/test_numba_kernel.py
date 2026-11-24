"""Restricted Numba kernels are checked, bounded and exact on the native engine."""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine, native
from src.core.engine import kernel as K
from src.core.types import Side, Split

needs_numba = pytest.mark.skipif(importlib.util.find_spec("numba") is None, reason="numba is not installed")

GOOD = """
def kernel(event, book, state, orders):
    state[0] += 1.0
    if state[0] % 10.0 != 0.0 or book[B_OK] == 0.0:
        return 0
    if event[E_N_WORKING] > 0:
        orders[0, O_OP] = OP_CANCEL
        orders[0, O_ORDER_ID] = event[E_WORKING]
        return 1
    orders[0, O_OP] = OP_BUY
    orders[0, O_KIND] = KIND_LIMIT
    orders[0, O_TIF] = TIF_DAY
    orders[0, O_SIZE] = 1.0
    orders[0, O_LIMIT] = book[B_BID_PX]
    return 1
"""


def test_a_valid_kernel_is_accepted():
    K.check(GOOD)


@pytest.mark.parametrize("bad", [
    "import os\ndef kernel(event, book, state, orders):\n    return 0\n",
    "def kernel(event, book, state, orders):\n    while True:\n        pass\n",
    "def kernel(event, book, state, orders):\n    event[0] = 1\n    return 0\n",
    "def kernel(event, book, state, orders):\n    book[0] = 1.0\n    return 0\n",
    "def kernel(event, book, state, orders):\n    return state.__class__\n",
    "def kernel(event, book, state, orders):\n    return open(0)\n",
    "def kernel(event, book, state, orders):\n    for i in range(1000000):\n        pass\n    return 0\n",
    "def kernel(event, book, state, orders):\n    for a in range(9):\n        for b in range(9):\n            for c in range(9):\n                pass\n    return 0\n",
    "def kernel(event, book, state):\n    return 0\n",
    "def kernel(event, book, state, orders):\n    return secret\n",
    "def kernel(event, book, state, orders):\n    event = state\n    return 0\n",
    "x = 1\ndef kernel(event, book, state, orders):\n    return 0\n",
])
def test_outside_the_language_is_refused(bad):
    with pytest.raises(K.KernelRejected):
        K.check(bad)




def call(kernel):
    event = np.zeros(41, np.int64)
    book = np.zeros(24)
    orders = np.zeros((4, 6))
    return kernel._entry.ctypes(event.ctypes.data_as(kernel._entry.ctypes.argtypes[0]),
                                book.ctypes.data_as(kernel._entry.ctypes.argtypes[1]),
                                kernel.state.ctypes.data_as(kernel._entry.ctypes.argtypes[2]),
                                orders.ctypes.data_as(kernel._entry.ctypes.argtypes[3]), kernel.state.size, 4)


@needs_numba
@pytest.mark.parametrize("line", ["return int(event[41])", "state[64] = 1.0\n    return 0",
                                  "orders[4, 0] = 1.0\n    return 0", "return int(book[24])"])
def test_an_out_of_bounds_read_or_write_stops_the_run(line):
    kernel = K.CompiledKernel(f"def kernel(event, book, state, orders):\n    {line}\n", state_size=64)
    assert call(kernel) == -1


@needs_numba
def test_a_compiled_kernel_returns_its_count():
    kernel = K.CompiledKernel("def kernel(event, book, state, orders):\n    state[0] += 1.0\n    return 0\n")
    assert call(kernel) == 0 and kernel.state[0] == 1.0


BOOK_KERNEL = """
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


class BookParityStrategy:
    """The same decisions as BOOK_KERNEL, through the Python engine and `ctx.book()`."""

    name = "check/kernel-parity"

    def __init__(self):
        self.count, self.checksum, self.missing = 0, 0.0, 0

    def setup(self, ctx):
        ctx.subscribe("tick")
        ctx.subscribe("book")

    def on_tick(self, ctx):
        self.count += 1
        book = ctx.book()
        if book is None:
            self.missing += 1
            return
        for px, qty in (*zip(book.bid_prices, book.bid_sizes), *zip(book.ask_prices, book.ask_sizes)):
            self.checksum = (self.checksum * 31.0 + px * float(qty)) % 1000000007.0
        if self.count % 50:
            return
        step = self.count // 50
        working = ctx.working_order_ids
        if working and step % 7 == 0:
            ctx.cancel(working[0])
            return
        if len(working) >= 4:
            return
        position = ctx.position
        buy = position < 0 or (position == 0 and step % 2 == 0)
        place = ctx.buy if buy else ctx.sell
        tif = "GTC" if step % 3 == 0 else "DAY"
        if step % 5 == 0:
            place(20, tif=tif)
        else:
            place(20, type="limit", limit=book.bid_prices[0] if buy else book.ask_prices[0], tif=tif)

    def on_bars(self, ctx):
        pass

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass


@needs_numba
@pytest.mark.skipif(not native.LIBRARY.exists(), reason="the native library is not built")
def test_a_book_reading_kernel_matches_the_python_engine():
    from test_native_event_loop import native_book

    spec = SyntheticSpec(n_sessions=40, ticks_per_session=240, tick_spacing_ns=5_000_000_000, session_gap_ns=0,
                         start_price=30000.0, tick_size=0.1, vol_per_tick=6.0, early_close_rate=0.0, length_jitter=0.0,
                         session_open_utc="00:00", size_scale=20, book=True, seed=5)
    dataset = SyntheticDataset(spec)
    strategy = BookParityStrategy()
    engine = Engine.for_test(dataset, strategy, split=Split.VALIDATION,
                    fill_model="book-queue-pessimistic", warmup_sessions=0, seed=5, commission_bps=2.0,
                    maker_commission_bps=0.0, latency_ns=1_000_000, book_features=True)
    engine.run()
    lib = native.load()
    kernel = K.CompiledKernel(BOOK_KERNEL)
    with native.NativeEngine(lib, latency_ns=1_000_000, require_two_sided=False, book_features=True) as fast:
        for session in dataset.scored_sessions(Split.VALIDATION, 0):
            ticks = dataset._session_ticks(session)
            trades = (np.array([t.ts for t in ticks], np.int64), np.array([t.price for t in ticks], float),
                      np.array([t.size for t in ticks], np.int64),
                      np.array([1 if t.side is Side.BUY else -1 if t.side is Side.SELL else 0 for t in ticks], np.int8))
            fast.run_session(native_book(dataset, session), trades, session, dataset.session_bounds(session)[1],
                             callback=kernel.callback(lib), user=kernel.user_pointer())
        assert kernel.error == 0 and len(engine.fill_log) > 5
        assert kernel.state[0] == strategy.count
        assert kernel.state[1] == strategy.checksum and kernel.state[2] == strategy.missing
        assert fast.fill_digest() == engine.fill_log_digest() and fast.position == engine.position


class BookEventParityStrategy(BookParityStrategy):
    """The same decisions, also taken on every book update."""

    name = "check/book-event-parity"

    def on_book_event(self, ctx):
        self.on_tick(ctx)


@needs_numba
@pytest.mark.skipif(not native.LIBRARY.exists(), reason="the native library is not built")
@pytest.mark.parametrize("latency", [0, 1_000_000, 45_000_000_000])
def test_book_update_events_match_the_python_engine(latency):
    from test_native_event_loop import native_book

    spec = SyntheticSpec(n_sessions=40, ticks_per_session=240, tick_spacing_ns=5_000_000_000, session_gap_ns=0,
                         start_price=30000.0, tick_size=0.1, vol_per_tick=6.0, early_close_rate=0.0, length_jitter=0.0,
                         session_open_utc="00:00", size_scale=20, book=True, seed=5)
    dataset = SyntheticDataset(spec)
    strategy = BookEventParityStrategy()
    engine = Engine.for_test(dataset, strategy, split=Split.VALIDATION,
                    fill_model="book-queue-pessimistic", warmup_sessions=0, seed=5, commission_bps=2.0,
                    maker_commission_bps=0.0, latency_ns=latency, book_features=True, book_events=True)
    engine.run()
    lib = native.load()
    kernel = K.CompiledKernel(BOOK_KERNEL)
    trades_seen = 0
    with native.NativeEngine(lib, latency_ns=latency, require_two_sided=False, book_features=True,
                             book_events=True) as fast:
        for session in dataset.scored_sessions(Split.VALIDATION, 0):
            ticks = dataset._session_ticks(session)
            trades_seen += len(ticks)
            trades = (np.array([t.ts for t in ticks], np.int64), np.array([t.price for t in ticks], float),
                      np.array([t.size for t in ticks], np.int64),
                      np.array([1 if t.side is Side.BUY else -1 if t.side is Side.SELL else 0 for t in ticks], np.int8))
            fast.run_session(native_book(dataset, session), trades, session, dataset.session_bounds(session)[1],
                             callback=kernel.callback(lib), user=kernel.user_pointer())
        assert kernel.error == 0 and strategy.count > trades_seen and len(engine.fill_log) > 5
        assert kernel.state[0] == strategy.count
        assert kernel.state[1] == strategy.checksum and kernel.state[2] == strategy.missing
        assert fast.fill_digest() == engine.fill_log_digest() and fast.position == engine.position


def test_book_events_refuse_a_strategy_without_the_callback():
    spec = SyntheticSpec(n_sessions=40, ticks_per_session=20, tick_spacing_ns=5_000_000_000, session_gap_ns=0,
                         start_price=30000.0, tick_size=0.1, vol_per_tick=6.0, early_close_rate=0.0, length_jitter=0.0,
                         session_open_utc="00:00", size_scale=20, book=True, seed=5)
    engine = Engine.for_test(SyntheticDataset(spec), BookParityStrategy(), split=Split.VALIDATION, fill_model="book-queue-pessimistic", warmup_sessions=0, seed=5,
                    commission_bps=2.0, maker_commission_bps=0.0, book_features=True, book_events=True)
    with pytest.raises(ValueError, match="on_book_event"):
        engine.run()


@needs_numba
@pytest.mark.skipif(not native.LIBRARY.exists(), reason="the native library is not built")
def test_native_fills_score_identically_through_the_unchanged_scorer():
    import json

    from src.core.scoring.scorer import score
    from test_native_event_loop import native_book

    spec = SyntheticSpec(n_sessions=120, ticks_per_session=240, tick_spacing_ns=5_000_000_000, session_gap_ns=0,
                         start_price=30000.0, tick_size=0.1, vol_per_tick=6.0, early_close_rate=0.0, length_jitter=0.0,
                         session_open_utc="00:00", size_scale=20, book=True, seed=5)
    dataset = SyntheticDataset(spec)
    engine = Engine.for_test(dataset, BookEventParityStrategy(), split=Split.VALIDATION, fill_model="book-queue-pessimistic", warmup_sessions=0, seed=5,
                    commission_bps=2.0, maker_commission_bps=0.0, latency_ns=1_000_000, book_features=True,
                    book_events=True)
    engine.run()
    lib = native.load()
    kernel = K.CompiledKernel(BOOK_KERNEL)
    period = engine.session_calendar()
    terms = dict(notional_base=30000.0, idle_charge=0.0, funding=None)
    costs = dict(commission_per_contract=engine.commission, commission_bps=2.0, maker_commission_bps=0.0)
    with native.NativeEngine(lib, latency_ns=1_000_000, require_two_sided=False, book_features=True,
                             book_events=True) as fast:
        for session in dataset.scored_sessions(Split.VALIDATION, 0):
            ticks = dataset._session_ticks(session)
            trades = (np.array([t.ts for t in ticks], np.int64), np.array([t.price for t in ticks], float),
                      np.array([t.size for t in ticks], np.int64),
                      np.array([1 if t.side is Side.BUY else -1 if t.side is Side.SELL else 0 for t in ticks], np.int8))
            fast.run_session(native_book(dataset, session), trades, session, dataset.session_bounds(session)[1],
                             callback=kernel.callback(lib), user=kernel.user_pointer())
        blotter, result = fast.scored(costs=costs, period=period, terms=terms)
    reference = engine.reconstructed()
    assert len(reference.rows) > 5 and blotter.exact_digest() == reference.exact_digest()
    expected = score(reference, period=period, notional_base=30000.0, idle_charge=0.0, funding=None)
    assert json.dumps(result.as_dict(), sort_keys=True) == json.dumps(expected.as_dict(), sort_keys=True)
