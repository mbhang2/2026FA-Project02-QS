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


@pytest.mark.parametrize("change", [dict(split="holdout"), dict(fill_model="favorable"),
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


def test_the_audit_window_serves_one_slice_through_the_columns_path():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_native_trial as R
    from src.core.data.columns import SessionColumns
    from src.core.engine.engine import Engine as E

    class Columns:
        name = "fake"

        def session_columns(self, split, *, warmup=0):
            for session in (3, 4, 5):
                n = 50
                yield SessionColumns(session, np.arange(n, dtype=np.int64) + session * 1000, np.full(n, 100.0),
                                     np.ones(n, np.int64), np.ones(n, np.int8))

    window = R._AuditWindow(Columns(), [(4, 10, 7)])
    assert E._columns_source(window) is not None
    walked = [(c.session, int(c.ts[0]), len(c)) for c in window.session_columns(Split.VALIDATION)]
    assert walked == [(4, 4010, 7)]
    assert sum(1 for _ in window.ticks(Split.VALIDATION)) == 7
    assert [(c.session, len(c)) for c in R._AuditWindow(Columns(), [(5, 45, None)]).session_columns(Split.TRAIN)] == [(5, 5)]
    across = R._AuditWindow(Columns(), [(3, 48, None), (4, 0, None)]).session_columns(Split.TRAIN)
    assert [(c.session, len(c)) for c in across] == [(3, 2), (4, 50)]

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
    (folder / "c.json").write_text("")
    assert R.recover_open_trials(ledger, lambda r, e, c: r) == 2
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [r["trial_id"] for r in rows] == ["b", "a", "c"]
    assert rows[1]["outcome"] == "crashed" and rows[1]["failure_kind"] == "killed_without_row"
    assert rows[2]["unreadable_entry"] is True
    assert not list(folder.glob("*.json"))
    assert R.recover_open_trials(ledger, lambda r, e, c: r) == 0


def _runner():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_native_trial as R

    return R


def test_a_live_trial_is_not_recovered_and_its_source_is_kept(tmp_path):
    import json

    R = _runner()
    ledger = tmp_path / "ledger.jsonl"
    entry, copy, nonce, handle = R.open_trial(ledger, "live", "source text", "p", "check")
    try:
        assert R.recover_open_trials(ledger, lambda r, e, c: r) == 0
        assert entry.is_file() and copy.read_text() == "source text"
        assert json.loads(entry.read_text())["nonce"] == nonce
    finally:
        assert R.close_trial(ledger, "live", entry, copy, handle) is False
    assert entry.is_file()
    assert R.recover_open_trials(ledger, lambda r, e, c: {**r, "kept": c.read_text()}) == 1
    row = json.loads(ledger.read_text())
    assert row["trial_id"] == "live" and row["kept"] == "source text" and "nonce" not in row


def test_a_ledger_run_that_fails_to_write_is_not_marked_written(tmp_path):
    from src.core.runlog import LedgerRun

    run_log = LedgerRun(tmp_path / "ledger.jsonl", row_builder=lambda r: {"bad": np.int64(1)})
    with pytest.raises(TypeError):
        with run_log:
            pass
    assert run_log.written is False


def test_the_child_refuses_a_forged_entry(tmp_path):
    import json

    R = _runner()
    ledger = R.SCRATCH / "test-child-gate" / "ledger.jsonl"
    entry, copy, nonce, handle = R.open_trial(ledger, "t1", T.CHECK_KERNEL, "p", "check")
    try:
        good = {"ledger": str(ledger.resolve()), "journal": str(entry), "trial_id": "t1", "source": T.CHECK_KERNEL,
                "nonce": nonce}
        assert R._check_child_spec(good).kind == "native"
        forged_dir = tmp_path / R.OPEN_DIR
        forged_dir.mkdir()
        (forged_dir / "t1.json").write_text(entry.read_text())
        for bad in ({**good, "nonce": "0" * 32}, {**good, "journal": str(forged_dir / "t1.json")},
                    {**good, "source": T.CHECK_KERNEL + "\n# changed\n"}, {**good, "trial_id": "t2"}):
            with pytest.raises((T.NativeTrialRefused, KeyError, FileNotFoundError)):
                R._check_child_spec(bad)
    finally:
        handle.close()
    with pytest.raises(T.NativeTrialRefused):
        R._check_child_spec(good)                     # no live parent holds the entry now
    entry.unlink()
    copy.unlink()


def _snapshot(**change):
    base = {"seq": 9, "position": 3, "next_id": 4, "trade": 5, "last_price": 100.0,
            "order_ints": np.array([[3, 1, 0, 1, 5, 2, 4, 0, 0, 0, 1, 7, 2, 1]], np.int64),
            "order_doubles": np.array([[99.5, 4.0]]), "state": np.zeros(64)}
    base.update(change)
    return base


def _fills():
    c = columns(3)
    c["order_id"] = np.array([1, 3, 3], np.int64)
    c["size"] = np.array([1, 1, 1], np.int64)
    c["seq"] = np.array([2, 5, 8], np.int64)
    return c


def test_a_consistent_snapshot_passes():
    T.check_snapshot(_snapshot(), _fills(), 5)


@pytest.mark.parametrize("change", [dict(position=10 ** 15), dict(last_price=float("nan")), dict(next_id=3),
                                    dict(order_doubles=np.array([[-1.0, 4.0]])),
                                    dict(order_doubles=np.array([[99.5, -1e9]])),
                                    dict(order_ints=np.array([[3, 1, 0, 1, 5, 99, 4, 0, 0, 0, 1, 7, 2, 1]], np.int64)),
                                    dict(order_ints=np.array([[3, 1, 0, 1, 5, 2, 4, 0, 0, 0, 1, 7, -7, 1]], np.int64)),
                                    dict(order_ints=np.array([[3, 2, 0, 1, 5, 2, 4, 0, 0, 0, 1, 7, 2, 1]], np.int64))])
def test_an_inconsistent_snapshot_is_refused(change):
    with pytest.raises(T.NativeTrialFailed):
        T.check_snapshot(_snapshot(**change), _fills(), 5)


class _Book:
    def __init__(self, ts):
        self.ts = ts


class _Columns:
    name = "fake"

    def __init__(self, sizes):
        self.sizes = sizes

    def session_columns(self, split, *, warmup=0):
        from src.core.data.columns import SessionColumns

        for session, n in self.sizes.items():
            ts = session * 10 ** 12 + np.arange(n, dtype=np.int64) * 10_000_000
            yield SessionColumns(session, ts, np.full(n, 100.0), np.ones(n, np.int64), np.ones(n, np.int8))

    def book_for(self, session):
        return _Book(session * 10 ** 12 + np.arange(0, 10 ** 12, 5_000_000_000, dtype=np.int64)[:10])


def test_short_windows_cross_sessions_and_cover_openings():
    import random

    R = _runner()
    R.SHORT_AUDIT_EVENTS = 30
    data = _Columns({1: 40, 2: 40, 3: 40})
    seen_open = False
    for seed in range(60):
        [window] = R.plan_windows(data, [1, 2, 3], False, random.Random(seed))
        assert all(0 <= start < 40 for _, start, _ in window)
        sessions = [s for s, _, _ in window]
        assert sessions == sorted(sessions) and len(set(sessions)) == len(sessions)
        seen_open |= len(window) > 1 and window[1][1] == 0
    assert seen_open


def test_deep_windows_start_at_the_previous_close():
    import random

    R = _runner()
    windows = R.plan_windows(_Columns({1: 40, 2: 40, 3: 40, 4: 40, 5: 40, 6: 40}), [1, 2, 3, 4, 5, 6], True,
                             random.Random(3))
    assert len(windows) == 5
    starts = [w[0][0] for w in windows]
    assert len(set(starts)) == len(starts)
    for w in windows:
        assert w[-1][1] == 0 and w[-1][2] is None
        if len(w) == 2:
            assert w[0][1] == 39 and w[0][0] + 1 == w[1][0]


def test_validation_needs_a_matching_deep_audit(tmp_path):
    import json

    ledger = tmp_path / "ledger.jsonl"
    good = {"engine": "native", "outcome": "completed", "audit_of": "t1", "kernel_sha256": "k", "profile": "p",
            "audit": {"tier": "deep", "matched": True, "windows": [{"fills": 3}]}}
    assert not T.deep_audit_passed(ledger, "t1", "k", "p")
    rows = [{**good, "audit": {**good["audit"], "tier": "short"}}, {**good, "outcome": "crashed"},
            {**good, "kernel_sha256": "other"}, {**good, "audit_of": "t2"}, {**good, "profile": "q"},
            {**good, "audit": {"tier": "deep", "matched": True, "windows": []}}]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert not T.deep_audit_passed(ledger, "t1", "k", "p")
    with ledger.open("a") as fh:
        fh.write(json.dumps(good) + "\n")
    assert T.deep_audit_passed(ledger, "t1", "k", "p")


def test_validation_is_allowed_and_the_holdout_is_not():
    assert T.refusal({**GOOD, "split": "validation"}) is None
    assert "Python engine only" in T.refusal({**GOOD, "split": "holdout"})


def test_the_market_making_profiles_are_as_approved():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import run

    profiles = run.load_config()["profiles"]
    for name, split in (("btc-okx-mm-train", "train"), ("btc-okx-mm-validation", "validation")):
        p = profiles[name]
        assert (p["split"], p["engine"], p["book_events"], p["inventory_cap_lots"], p["gate"]) == \
            (split, "native", True, 1000, "spread_capture")
        assert (p["maker_commission_bps"], p["commission_bps"], p["latency_ns"]) == (0.0, 2.0, 1_000_000)
        assert "Part D" in T.refusal(p)


@pytest.mark.parametrize("entry,message", [
    (dict(engine="python"), "engine must be"),
    (dict(engine="native", book_events=True, inventory_cap_lots=0), "inventory_cap_lots"),
    (dict(engine="native", book_events=True, inventory_cap_lots=True), "inventory_cap_lots"),
    (dict(engine="native", book_events=True, gate="sign_flip"), "gate must be"),
    (dict(inventory_cap_lots=10), "need an engine of"),
    (dict(engine="native"), "needs book_features and book_events"),
])
def test_bad_native_profiles_are_refused(tmp_path, entry, message):
    import yaml

    from src.core.backtest_config import BacktestConfigError, compile_backtests

    base = dict(dataset="d", split="train", use_quotes=True, book_features=True, costs="c")
    path = tmp_path / "backtest.yaml"
    path.write_text(yaml.safe_dump({"backtests": {"x": {**base, **entry}}}))
    with pytest.raises(BacktestConfigError, match=message):
        compile_backtests({"profiles": {}, "datasets": {"d": {"book": True}}}, path)


def test_run_py_refuses_a_native_profile():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, str(root / "run.py"), "--profile", "btc-okx-mm-train", "--quiet"],
                          cwd=root, capture_output=True, text=True, timeout=120)
    assert done.returncode != 0 and "run_native_trial.py" in done.stderr


def test_native_validation_needs_a_deep_audit(tmp_path):
    R = _runner()
    ledger = R.SCRATCH / "test-validation-gate" / "ledger.jsonl"
    kernel = tmp_path / "k.py"
    kernel.write_text(T.CHECK_KERNEL)
    with pytest.raises(T.NativeTrialRefused, match="names the train trial"):
        R.run_trial("btc-okx-mm-validation", kernel, ledger)
    with pytest.raises(T.NativeTrialRefused, match="no matching deep audit"):
        R.run_trial("btc-okx-mm-validation", kernel, ledger, validates="t1", train_profile="btc-okx-mm-train")
    with pytest.raises(T.NativeTrialRefused, match="only a validation run"):
        R.run_trial("btc-okx-mm-train", kernel, ledger, validates="t1")
    assert not ledger.exists()


def test_the_hft_native_profiles_have_no_cap_or_gate_and_run():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import run

    profiles = run.load_config()["profiles"]
    for name, split, engine in (("btc-okx-hft-native-train", "train", "native"),
                                ("btc-okx-hft-native-validation", "validation", "native"),
                                ("btc-okx-hft-native-holdout", "holdout", "reference")):
        p = profiles[name]
        assert (p["split"], p["engine"], p["book_events"]) == (split, engine, True)
        assert "gate" not in p and "inventory_cap_lots" not in p
        assert (p["maker_commission_bps"], p["commission_bps"], p["latency_ns"]) == (0.0, 2.0, 1_000_000)
    assert T.refusal(profiles["btc-okx-hft-native-train"]) is None
    assert T.refusal(profiles["btc-okx-hft-native-validation"]) is None


def test_native_fill_logs_are_kept_past_the_default_cap(tmp_path):
    from src.core.runlog import MAX_STORED_FILLS, store_fill_log

    fills = [object()] * (MAX_STORED_FILLS + 1)
    assert store_fill_log(tmp_path / "ledger.jsonl", "d", fills) == ""
