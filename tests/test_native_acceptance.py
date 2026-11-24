"""Amendment 24 acceptance: no lookahead, and an audit that starts after warmup is exact."""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from src.core.data.okx_book import SessionBook
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine, native
from src.core.runlog import RunTicket
from src.core.engine import kernel as K
from src.core.engine import native_trial as T
from src.core.engine.reconstruct import log_digest
from src.core.types import Side, Split

pytestmark = pytest.mark.skipif(importlib.util.find_spec("numba") is None or not native.LIBRARY.exists(),
                                reason="numba or the native library is missing")

SPEC = dict(n_sessions=60, ticks_per_session=240, tick_spacing_ns=5_000_000_000, session_gap_ns=0,
            start_price=30000.0, tick_size=0.1, vol_per_tick=6.0, early_close_rate=0.0, length_jitter=0.0,
            session_open_utc="00:00", size_scale=20, book=True, seed=5)

# Folds every number an event shows into state[1], and stops folding after the cut.
OBSERVER = """
def kernel(event, book, state, orders):
    if event[E_TS] > CUT:
        return 0
    state[0] += 1.0
    for i in range(41):
        state[1] = (state[1] * 31.0 + float(event[i])) % 1000000007.0
    for i in range(24):
        state[1] = (state[1] * 31.0 + book[i] * 7.0) % 1000000007.0
    return 0
"""


def _trades(dataset, session):
    ticks = dataset._session_ticks(session)
    return (np.array([t.ts for t in ticks], np.int64), np.array([t.price for t in ticks], float),
            np.array([t.size for t in ticks], np.int64),
            np.array([1 if t.side is Side.BUY else -1 if t.side is Side.SELL else 0 for t in ticks], np.int8))


def _native_book(dataset, session):
    from test_native_event_loop import native_book

    return native_book(dataset, session)


def _scramble(book: SessionBook, trades: tuple, cut: int, rng) -> tuple[SessionBook, tuple]:
    """Random values in every book row from the cut's millisecond on, and every trade after the cut."""
    rows = np.asarray(book.ts) >= cut - cut % 1_000_000
    bid, ask = np.array(book.bid, float), np.array(book.ask, float)
    bid_qty, ask_qty = np.array(book.bid_qty, np.int64), np.array(book.ask_qty, np.int64)
    n = int(rows.sum())
    bid[rows] = rng.uniform(1.0, 90000.0, n)
    ask[rows] = bid[rows] + rng.uniform(0.1, 50.0, n)
    bid_qty[rows], ask_qty[rows] = rng.integers(1, 5000, n), rng.integers(1, 5000, n)
    depth = {"scale": book._depth["scale"]}
    for name in ("bid_px", "ask_px", "bid_lots", "ask_lots"):
        values = np.array(book._depth[name], np.int32)
        high = 900_000 if name.endswith("px") else 5000
        values[rows] = rng.integers(1, high, values[rows].shape).astype(np.int32)
        depth[name] = values
    ts, price, size, side = (np.array(a) for a in trades)
    later = ts > cut
    price[later] = rng.uniform(1.0, 90000.0, int(later.sum()))
    size[later] = rng.integers(1, 5000, int(later.sum()))
    side[later] = rng.choice(np.array([-1, 1], np.int8), int(later.sum()))
    return SessionBook(np.asarray(book.ts), bid, ask, bid_qty, ask_qty, depth=depth), (ts, price, size, side)


def _run(lib, source, book, trades, session, close_ts, latency):
    kernel = K.CompiledKernel(source)
    with native.NativeEngine(lib, ticket=RunTicket.for_test(), latency_ns=latency, require_two_sided=False, book_features=True,
                             book_events=True) as fast:
        fast.run_session(book, trades, session, close_ts, callback=kernel.callback(lib), user=kernel.user_pointer())
        return kernel.state.copy(), fast.fill_columns()


@pytest.mark.parametrize("fraction", [0.2, 0.5, 0.9])
@pytest.mark.parametrize("latency", [0, 1_000_000])
def test_no_event_sees_data_after_its_visible_book(fraction, latency):
    dataset = SyntheticDataset(SyntheticSpec(**SPEC))
    session = dataset.scored_sessions(Split.VALIDATION, 0)[0]
    trades = _trades(dataset, session)
    cut = int(trades[0][int(fraction * (trades[0].size - 1))])
    book = _native_book(dataset, session)
    close = dataset.session_bounds(session)[1]
    lib = native.load()
    observer = OBSERVER.replace("CUT", str(cut))
    clean, _ = _run(lib, observer, book, trades, session, close, latency)
    for seed in range(3):
        noisy_book, noisy_trades = _scramble(book, trades, cut, np.random.default_rng(seed))
        noisy, _ = _run(lib, observer, noisy_book, noisy_trades, session, close, latency)
        assert clean[0] > 10 and clean.tolist() == noisy.tolist()


@pytest.mark.parametrize("latency", [0, 1_000_000, 100_000_000])
def test_fills_before_the_cut_do_not_depend_on_later_data(latency):
    dataset = SyntheticDataset(SyntheticSpec(**SPEC))
    session = dataset.scored_sessions(Split.VALIDATION, 0)[0]
    trades = _trades(dataset, session)
    cut = int(trades[0][int(0.85 * (trades[0].size - 1))])
    book = _native_book(dataset, session)
    close = dataset.session_bounds(session)[1]
    lib = native.load()
    _, clean = _run(lib, T.CHECK_KERNEL, book, trades, session, close, latency)
    before = clean["ts"] < cut - cut % 1_000_000
    assert before.sum() > 5
    for seed in range(3):
        noisy_book, noisy_trades = _scramble(book, trades, cut, np.random.default_rng(seed))
        _, noisy = _run(lib, T.CHECK_KERNEL, noisy_book, noisy_trades, session, close, latency)
        kept = noisy["ts"] < cut - cut % 1_000_000
        for name in clean:
            assert clean[name][before].tolist() == noisy[name][kept].tolist(), name


class _Only:
    """The scored sessions, with no warmup, through the tick path."""

    def __init__(self, inner, sessions):
        self._inner, self._kept, self.name = inner, set(sessions), inner.name

    def __getattr__(self, key):
        return getattr(self._inner, key)

    def ticks(self, split, *, warmup=0):
        for tick in self._inner.ticks(split, warmup=0):
            if tick.session in self._kept:
                yield tick


def _columns(log, shift=0):
    return {"seq": np.array([f.seq + shift for f in log], np.int64), "ts": np.array([f.ts for f in log], np.int64),
            "session": np.array([f.session for f in log], np.int64),
            "order_id": np.array([f.order_id for f in log], np.int64),
            "side": np.array([1 if f.side is Side.BUY else -1 for f in log], np.int8),
            "size": np.array([f.size for f in log], np.int64), "price": np.array([f.price for f in log], np.float64),
            "maker": np.array([1 if f.liquidity == "maker" else 0 for f in log], np.int8),
            "mid": np.array([f.mid for f in log], np.float64)}


def _strategy(state):
    namespace = {}
    exec(T.audit_strategy_source(T.CHECK_KERNEL, state), namespace)
    return namespace["Strategy"]()


@pytest.mark.parametrize("latency", [1_000_000, 100_000_000])
def test_an_audit_started_after_warmup_equals_replaying_warmup(latency):
    dataset = SyntheticDataset(SyntheticSpec(**SPEC))
    warmup = 3
    warm = sorted(dataset.warmup_sessions(Split.VALIDATION, warmup))
    scored = dataset.scored_sessions(Split.VALIDATION, 0)
    assert len(warm) == warmup and scored
    lib = native.load()
    kernel = K.CompiledKernel(T.CHECK_KERNEL)
    with native.NativeEngine(lib, ticket=RunTicket.for_test(), latency_ns=latency, require_two_sided=False, book_features=True,
                             book_events=True) as fast:
        state, first_seq = None, None
        for session in warm + list(scored):
            if session not in warm and state is None:
                state, first_seq = kernel.state.copy(), fast.seq + 1
            fast.run_session(_native_book(dataset, session), _trades(dataset, session), session,
                             dataset.session_bounds(session)[1], actionable=session not in warm,
                             callback=kernel.callback(lib), user=kernel.user_pointer())
        columns = fast.fill_columns()
    assert state[0] > 0 and columns["seq"].size > 5
    common = dict(split=Split.VALIDATION, fill_model=T.BOOK_MODEL, seed=5, commission_bps=2.0,
                  maker_commission_bps=0.0, latency_ns=latency, book_features=True, book_events=True)
    replayed = Engine.for_test(dataset, _strategy(np.zeros(64)), warmup_sessions=warmup, **common)
    replayed.run()
    started = Engine.for_test(_Only(dataset, scored), _strategy(state), warmup_sessions=0, **common)
    started.run()
    native_digest = log_digest(T.records_from_columns(columns))
    assert log_digest(T.records_from_columns(_columns(replayed.fill_log))) == native_digest
    assert log_digest(T.records_from_columns(_columns(started.fill_log, first_seq))) == native_digest


class _Slice:
    """One session, from one trade on, through the tick path."""

    def __init__(self, inner, session, start):
        self._inner, self._session, self._start, self.name = inner, session, start, inner.name

    def __getattr__(self, key):
        return getattr(self._inner, key)

    def ticks(self, split, *, warmup=0):
        ticks = [t for t in self._inner.ticks(split, warmup=0) if t.session == self._session]
        yield from ticks[self._start:]


@pytest.mark.parametrize("latency", [0, 1_000_000, 100_000_000])
@pytest.mark.parametrize("pick", [0, 1, 2, 3])
def test_a_python_run_resumed_mid_session_matches_the_native_engine(latency, pick):
    """Amendment 24 B1: the audit may start anywhere, so resuming must be exact."""
    dataset = SyntheticDataset(SyntheticSpec(**SPEC))
    scored = dataset.scored_sessions(Split.VALIDATION, 0)
    rng = np.random.default_rng(100 + pick)
    target = scored[int(rng.integers(2, len(scored)))]
    target_trades = _trades(dataset, target)
    start = 0 if pick == 0 else int(rng.integers(1, target_trades[0].size))
    lib = native.load()
    kernel = K.CompiledKernel(T.CHECK_KERNEL)
    with native.NativeEngine(lib, ticket=RunTicket.for_test(), latency_ns=latency, require_two_sided=False,
                             book_features=True, book_events=True) as fast:
        for session in scored:
            snap = start if session == target else -1
            fast.run_session(_native_book(dataset, session), _trades(dataset, session), session,
                             dataset.session_bounds(session)[1], callback=kernel.callback(lib),
                             user=kernel.user_pointer(), snapshot_trade=snap, state=kernel.state)
            if session == target:
                saved = fast.snapshot()
            if session == target:
                break
        columns = fast.fill_columns()
    assert saved is not None and saved["trade"] == start
    after = int(target_trades[0][start - 1]) if start else int(target_trades[0][0])
    resume = T.resume_from_snapshot(saved, after)
    namespace = {}
    exec(T.audit_strategy_source(T.CHECK_KERNEL, saved["state"], saved["last_price"]), namespace)
    engine = Engine.for_test(_Slice(dataset, target, start), namespace["Strategy"](), split=Split.VALIDATION,
                             fill_model=T.BOOK_MODEL, warmup_sessions=0, seed=5, commission_bps=2.0,
                             maker_commission_bps=0.0, latency_ns=latency, book_features=True, book_events=True,
                             resume=resume)
    engine.run()
    first = resume["first_seq"]
    window = (columns["seq"] >= first) & (columns["session"] == target)
    native_part = T.records_from_columns({k: v[window] for k, v in columns.items()})
    python_part = T.records_from_columns(_columns(engine.fill_log, first))
    assert len(native_part) > 0 or pick == 3
    assert log_digest(python_part) == log_digest(native_part)
