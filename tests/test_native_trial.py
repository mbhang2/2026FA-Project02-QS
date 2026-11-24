"""The native trial path refuses what it has not proved and rejects forged fills."""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine, native
from src.core.runlog import RunTicket
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
                                    dict(book_features=False), dict(fit_sessions=2), dict(action_cap=32),
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
    namespace = {}
    exec(T.audit_strategy_source(BOOK_KERNEL, [0.0] * 64), namespace)
    engine = Engine.for_test(dataset, namespace["Strategy"](), split=Split.VALIDATION, fill_model=T.BOOK_MODEL, warmup_sessions=0, seed=5, commission_bps=2.0,
                    maker_commission_bps=0.0, latency_ns=1_000_000, book_features=True, book_events=True)
    engine.run()
    lib = native.load()
    kernel = K.CompiledKernel(BOOK_KERNEL)
    with native.NativeEngine(lib, ticket=RunTicket.for_test(), latency_ns=1_000_000, require_two_sided=False, book_features=True,
                             book_events=True) as fast:
        for session in dataset.scored_sessions(Split.VALIDATION, 0):
            ticks = dataset._session_ticks(session)
            trades = (np.array([t.ts for t in ticks], np.int64), np.array([t.price for t in ticks], float),
                      np.array([t.size for t in ticks], np.int64),
                      np.array([1 if t.side is Side.BUY else -1 if t.side is Side.SELL else 0 for t in ticks], np.int8))
            fast.run_session(native_book(dataset, session), trades, session, dataset.session_bounds(session)[1],
                             callback=kernel.callback(lib), user=kernel.user_pointer())
        assert len(engine.fill_log) > 5 and fast.fill_digest() == engine.fill_log_digest()


def test_the_event_cut_counts_trades_and_book_events():
    trades = np.array([1, 2, 3, 4], np.int64) * 1_000_000
    book = np.array([1, 2, 3], np.int64) * 1_000_000
    assert T.trades_for_events(trades, book, 1) == 1
    assert T.trades_for_events(trades, book, 3) == 2
    assert T.trades_for_events(trades, book, 100) == 4


def test_the_audit_state_round_trips_exactly():
    state = [0.1, -1.0 / 3.0, 1e300] + [0.0] * 61
    namespace = {}
    exec(T.audit_strategy_source(T.CHECK_KERNEL, state), namespace)
    assert namespace["Strategy"]().state.tolist() == state


def test_the_audit_wrapper_serves_only_its_sessions_through_the_columns_path():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_native_trial as R
    from src.core.engine.engine import Engine as E

    from src.core.data.columns import SessionColumns

    class Columns:
        name = "fake"

        def session_columns(self, split, *, warmup=0):
            for session in (3, 4, 5):
                n = 50
                yield SessionColumns(session, np.arange(n, dtype=np.int64), np.full(n, 100.0),
                                     np.ones(n, np.int64), np.ones(n, np.int8))

    sessions = [3, 4]
    wrapped = R._AuditSessions(Columns(), sessions, first_trades=7)
    assert E._columns_source(wrapped) is not None
    walked = [(c.session, len(c)) for c in wrapped.session_columns(Split.VALIDATION)]
    assert walked == [(sessions[0], 7), (sessions[1], 50)]
    assert sum(1 for _ in wrapped.ticks(Split.VALIDATION)) == 57


def test_the_audit_state_keeps_every_bit():
    state = np.array([np.nan, -np.nan, 1e300, -0.0] + [0.0] * 60)
    state[0] = np.frombuffer(np.uint64(0x7FF8000000000123).tobytes(), np.float64)[0]
    namespace = {}
    exec(T.audit_strategy_source(T.CHECK_KERNEL, state), namespace)
    assert namespace["Strategy"]().state.view(np.uint64).tolist() == state.view(np.uint64).tolist()


@pytest.mark.parametrize("name", ["int", "state", "B_PRICE"])
def test_a_loop_cannot_rebind_a_protected_name(name):
    with pytest.raises(K.KernelRejected):
        K.check(f"def kernel(event, book, state, orders):\n    for {name} in range(2):\n        pass\n    return 0\n")


@pytest.mark.skipif(importlib.util.find_spec("numba") is None, reason="numba is not installed")
@pytest.mark.parametrize("value", ["4294967297", "65", "-1", "0.0 * (1e308 * 10.0)"])
def test_an_out_of_range_action_count_stops_the_run(value):
    """`int(nan)` inside a kernel becomes 0 under Numba and raises in Python. That is not
    refusable here; the audit sees it as a crash, which still counts."""
    kernel = K.CompiledKernel(f"def kernel(event, book, state, orders):\n    return {value}\n")
    event, book, orders = np.zeros(41, np.int64), np.zeros(24), np.zeros((64, 6))
    call = kernel._entry.ctypes
    result = call(event.ctypes.data_as(call.argtypes[0]), book.ctypes.data_as(call.argtypes[1]),
                  kernel.state.ctypes.data_as(call.argtypes[2]), orders.ctypes.data_as(call.argtypes[3]),
                  kernel.state.size, 64)
    assert result == -1


def test_a_native_engine_needs_a_ticket():
    from src.core.runlog import RunNotAccounted

    if not native.LIBRARY.exists():
        pytest.skip("the native library is not built")
    with pytest.raises(TypeError):
        native.NativeEngine(native.load(), latency_ns=0)
    with pytest.raises(RunNotAccounted):
        native.NativeEngine(native.load(), ticket="forged", latency_ns=0)


def test_an_orphaned_trial_becomes_a_crashed_row(tmp_path):
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_native_trial as R

    ledger = tmp_path / "ledger.jsonl"
    folder = tmp_path / R.OPEN_DIR
    folder.mkdir()
    (folder / "a.json").write_text(json.dumps({"trial_id": "a", "kernel_sha256": "k", "profile": "p"}))
    (folder / "b.json").write_text(json.dumps({"trial_id": "b", "kernel_sha256": "k", "profile": "p"}))
    ledger.write_text(json.dumps({"trial_id": "b", "outcome": "completed"}) + "\n")
    assert R.recover_open_trials(ledger, lambda r: r) == 1
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [r["trial_id"] for r in rows] == ["b", "a"]
    assert rows[1]["outcome"] == "crashed" and rows[1]["failure_kind"] == "killed_without_row"
    assert not list(folder.glob("*.json"))
    assert R.recover_open_trials(ledger, lambda r: r) == 0
