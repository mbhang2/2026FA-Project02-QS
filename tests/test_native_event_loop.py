"""The native event loop reproduces the Python engine's fill log."""

from __future__ import annotations

import ctypes

import numpy as np
import pytest

from src.core.data.okx_book import SessionBook, encode_depth
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine, native
from src.core.runlog import RunTicket
from src.core.types import Side, Split

if not native.LIBRARY.exists():
    pytest.skip("the native library is not built", allow_module_level=True)


class ParityStrategy:
    name = "check/native-parity"

    def __init__(self, every, hold, offset, size, market_every):
        self.every, self.hold, self.offset, self.size, self.market_every = every, hold, offset, size, market_every
        self.count = 0

    def setup(self, ctx):
        ctx.subscribe("tick")

    def on_tick(self, ctx):
        self.count += 1
        n = self.count
        if n % self.every:
            return
        step = n // self.every
        working = ctx.working_order_ids
        if working and step % self.hold == 0:
            ctx.cancel(working[0])
            return
        if len(working) >= 4:
            return
        position = ctx.position
        buy = position < 0 or (position == 0 and step % 2 == 0)
        place = ctx.buy if buy else ctx.sell
        tif = "GTC" if step % 3 == 0 else "DAY"
        if step % self.market_every == 0:
            place(self.size, tif=tif)
        else:
            ticks = float(np.rint(ctx.tick.price * 10.0))
            place(self.size, type="limit", limit=(ticks + (-self.offset if buy else self.offset)) / 10.0, tif=tif)

    def on_bars(self, ctx):
        pass

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass


def native_book(dataset, session):
    book = dataset.book_for(session)
    deep = book._deep()
    stored = {"scale": 10}
    for side in ("bid", "ask"):
        stored[f"{side}_px"], stored[f"{side}_lots"] = encode_depth(deep[f"{side}_px"][:, 1:], deep[f"{side}_lots"][:, 1:], 10)
    return SessionBook(np.asarray(book.ts), deep["bid_px"][:, 0].copy(), deep["ask_px"][:, 0].copy(),
                       deep["bid_lots"][:, 0].astype(np.int64), deep["ask_lots"][:, 0].astype(np.int64), depth=stored)


@pytest.mark.parametrize("latency", [0, 1_000_000, 45_000_000_000])
def test_the_fill_log_matches_the_python_engine(latency):
    spec = SyntheticSpec(n_sessions=40, ticks_per_session=240, tick_spacing_ns=5_000_000_000, session_gap_ns=0,
                         start_price=30000.0, tick_size=0.1, vol_per_tick=6.0, early_close_rate=0.0, length_jitter=0.0,
                         session_open_utc="00:00", size_scale=20, book=True, seed=5)
    dataset = SyntheticDataset(spec)
    params = (7, 5, 3, 25, 4)
    engine = Engine(dataset, ParityStrategy(*params), ticket=RunTicket.for_calibration("native parity"),
                    split=Split.VALIDATION, fill_model="book-queue-pessimistic", warmup_sessions=0, seed=5,
                    commission_bps=2.0, maker_commission_bps=0.0 if latency else None, latency_ns=latency)
    engine.run()
    lib = native.load()
    state = native.ParityState(*params, 0)
    strategy = ctypes.cast(lib.fc_parity_strategy, ctypes.c_void_p)
    with native.NativeEngine(lib, latency_ns=latency, require_two_sided=False) as fast:
        for session in dataset.scored_sessions(Split.VALIDATION, 0):
            ticks = dataset._session_ticks(session)
            trades = (np.array([t.ts for t in ticks], np.int64), np.array([t.price for t in ticks], float),
                      np.array([t.size for t in ticks], np.int64),
                      np.array([1 if t.side is Side.BUY else -1 if t.side is Side.SELL else 0 for t in ticks], np.int8))
            fast.run_session(native_book(dataset, session), trades, session, dataset.session_bounds(session)[1],
                             callback=strategy, user=ctypes.byref(state))
        assert len(engine.fill_log) > 50
        assert fast.fill_digest() == engine.fill_log_digest()
        assert fast.position == engine.position
