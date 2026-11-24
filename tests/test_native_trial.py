"""The native trial path refuses what it has not proved and rejects forged fills."""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine, native
from src.core.engine import kernel as K
from src.core.engine import native_trial as T
from src.core.types import Side, Split

GOOD = dict(split="train", fill_model=T.BOOK_MODEL, book_features=True)


def columns(n=2):
    return dict(seq=np.arange(n, dtype=np.int64), ts=np.arange(10, 10 + n, dtype=np.int64),
                session=np.full(n, 7, np.int64), order_id=np.arange(1, n + 1, dtype=np.int64),
                side=np.ones(n, np.int8), size=np.ones(n, np.int64), price=np.full(n, 100.0),
                maker=np.zeros(n, np.int8), mid=np.full(n, 100.0))


@pytest.mark.parametrize("change", [dict(split="validation"), dict(split="holdout"), dict(fill_model="favorable"),
                                    dict(book_features=False), dict(warmup_sessions=1), dict(fit_sessions=2),
                                    dict(weights_path="w"), dict(allow_overnight=False)])
def test_unproved_profiles_are_refused(change):
    assert T.refusal({**GOOD, **change})


def test_a_proved_profile_is_accepted():
    assert T.refusal(GOOD) is None


def test_valid_columns_pass():
    T.validate_columns(columns(), {7}, lambda s: (0, 100))


@pytest.mark.parametrize("name,index,value", [("seq", 1, -1), ("order_id", 0, 0), ("side", 0, 0), ("size", 0, 0),
                                              ("maker", 0, 2), ("price", 0, np.nan), ("price", 0, -1.0),
                                              ("mid", 0, np.inf), ("session", 1, 8), ("ts", 1, 101)])
def test_forged_fills_are_rejected(name, index, value):
    bad = columns()
    bad[name][index] = value
    with pytest.raises(T.NativeTrialFailed):
        T.validate_columns(bad, {7}, lambda s: (0, 100))


def test_wrong_column_type_is_rejected():
    bad = columns()
    bad["size"] = bad["size"].astype(np.float64)
    with pytest.raises(T.NativeTrialFailed):
        T.validate_columns(bad, {7}, lambda s: (0, 100))


@pytest.mark.skipif(importlib.util.find_spec("numba") is None or not native.LIBRARY.exists(),
                    reason="numba or the native library is missing")
def test_the_audit_strategy_matches_the_native_engine():
    from test_native_event_loop import native_book
    from test_numba_kernel import BOOK_KERNEL

    spec = SyntheticSpec(n_sessions=40, ticks_per_session=240, tick_spacing_ns=5_000_000_000, session_gap_ns=0,
                         start_price=30000.0, tick_size=0.1, vol_per_tick=6.0, early_close_rate=0.0, length_jitter=0.0,
                         session_open_utc="00:00", size_scale=20, book=True, seed=5)
    dataset = SyntheticDataset(spec)
    engine = Engine.for_test(dataset, T.KernelStrategy(BOOK_KERNEL), split=Split.VALIDATION, fill_model=T.BOOK_MODEL, warmup_sessions=0, seed=5, commission_bps=2.0,
                    maker_commission_bps=0.0, latency_ns=1_000_000, book_features=True, book_events=True)
    engine.run()
    lib = native.load()
    kernel = K.CompiledKernel(BOOK_KERNEL)
    with native.NativeEngine(lib, latency_ns=1_000_000, require_two_sided=False, book_features=True,
                             book_events=True) as fast:
        for session in dataset.scored_sessions(Split.VALIDATION, 0):
            ticks = dataset._session_ticks(session)
            trades = (np.array([t.ts for t in ticks], np.int64), np.array([t.price for t in ticks], float),
                      np.array([t.size for t in ticks], np.int64),
                      np.array([1 if t.side is Side.BUY else -1 if t.side is Side.SELL else 0 for t in ticks], np.int8))
            fast.run_session(native_book(dataset, session), trades, session, dataset.session_bounds(session)[1],
                             callback=kernel.callback(lib), user=kernel.user_pointer())
        assert len(engine.fill_log) > 5 and fast.fill_digest() == engine.fill_log_digest()
