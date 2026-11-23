"""Amendment 22: a strategy may read the book visible before its own millisecond."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from src.core.backtest_config import BacktestConfigError, compile_backtests, prepare_config
from src.core.costs import load_registry
from src.core.data.okx_book import SessionBook
from src.core.engine.context import ContextError, EnginePort, BarContext, IntentQueue
from src.core.engine.engine import Engine
from src.core.types import Side

ROOT = Path(__file__).resolve().parents[1]
MS = 1_000_000


def book(ts, bid=100.0, ask=101.0):
    n = len(ts)
    arr = np.asarray(ts, dtype=np.int64)
    return SessionBook(arr, np.full(n, bid) + np.arange(n), np.full(n, ask) + np.arange(n),
                       np.arange(n, dtype=np.int64) + 10, np.arange(n, dtype=np.int64) + 20)


class FakeDataset:
    def __init__(self, session_book):
        self._book = session_book

    def book_for(self, session):
        return self._book


def snapshot_fn(session_book, *, requested=True):
    engine = Engine.__new__(Engine)
    engine.book_features = True
    engine.dataset = FakeDataset(session_book)
    engine._book_access = {"requested": requested}
    return engine._book_snapshot_fn()


def test_a_row_in_the_same_millisecond_is_hidden():
    snap = snapshot_fn(book([10 * MS, 20 * MS + 5]))
    assert snap(20 * MS + 900, 1).ts == 10 * MS
    assert snap(21 * MS, 1).ts == 20 * MS + 5


def test_no_earlier_row_is_none():
    assert snapshot_fn(book([10 * MS]))(10 * MS + 1, 1) is None


def test_a_stale_book_is_none():
    snap = snapshot_fn(book([10 * MS]))
    assert snap(10 * MS + 1_000_000_000, 1) is not None
    assert snap(10 * MS + 1_000_000_001, 1) is None


def test_the_view_holds_the_visible_row():
    got = snapshot_fn(book([10 * MS, 11 * MS]))(12 * MS, 1)
    assert (got.bid_prices, got.bid_sizes, got.ask_prices, got.ask_sizes) == (
        (101.0,), (11,), (102.0,), (21,))
    with pytest.raises(Exception):
        got.bid_sizes = (1,)


def test_later_rows_cannot_change_what_is_read():
    ts = [i * MS for i in range(1, 50)]
    first = book(ts)
    second = book(ts)
    second.bid[30:] = 1.0
    second.ask[30:] = 9.0
    second.bid_qty[30:] = 999
    second.ask_qty[30:] = 999
    for now in range(2, 31):
        assert snapshot_fn(first)(now * MS, 1) == snapshot_fn(second)(now * MS, 1)


def test_reading_without_asking_is_refused():
    with pytest.raises(ContextError):
        snapshot_fn(book([MS]), requested=False)(2 * MS, 1)


def test_a_run_without_the_feature_has_no_book_view():
    engine = Engine.__new__(Engine)
    engine.book_features = False
    assert engine._book_snapshot_fn() is None


def test_the_fee_tier_is_registered_and_btcusdt_is_unchanged():
    costs = load_registry()
    assert costs["BTCUSDT@hft-0-2"]["commission_bps"] == 2.0
    assert costs["BTCUSDT@hft-0-2"]["maker_commission_bps"] == 0.0
    assert costs["BTCUSDT"]["commission_bps"] == 5.0
    assert costs["BTCUSDT"]["maker_commission_bps"] == 5.0


def test_the_hft_profiles_have_the_registered_terms():
    import yaml
    cfg = prepare_config(yaml.safe_load((ROOT / "config" / "run.yaml").read_text()))
    for name in ("btc-okx-hft-train", "btc-okx-hft-validation"):
        p = cfg["profiles"][name]
        assert p["book_features"] is True and p["use_quotes"] is True
        assert p["latency_ns"] == 1_000_000
        assert (p["maker_commission_bps"], p["commission_bps"]) == (0.0, 2.0)
    assert not cfg["profiles"]["btc-okx-book-train"].get("book_features")


def test_book_features_needs_quotes(tmp_path):
    path = tmp_path / "backtest.yaml"
    path.write_text("backtests:\n  x:\n    dataset: d\n    split: train\n    use_quotes: false\n"
                    "    costs: BTCUSDT\n    book_features: true\n")
    with pytest.raises(BacktestConfigError):
        compile_backtests({"profiles": {}, "datasets": {"d": {}}}, path)


def _control():
    spec = importlib.util.spec_from_file_location("crypto_book_signal_control",
                                                  ROOT / "scripts" / "crypto_book_signal_control.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_control_constants_are_the_registered_ones():
    c = _control()
    assert c.THRESHOLDS == (0.2, 0.3, 0.4, 0.5, 0.6) and c.LEAN_ACCURACY == 0.70
    assert (c.CALIBRATION_SEEDS[0], len(c.CALIBRATION_SEEDS)) == (600_001, 100)
    assert (c.UNPROFITABLE_SEEDS[0], len(c.UNPROFITABLE_SEEDS)) == (601_001, 1_000)
    assert (c.NOISE_SEEDS[0], len(c.NOISE_SEEDS)) == (602_001, 400)
    assert (c.STRONG_SEEDS[0], len(c.STRONG_SEEDS)) == (602_401, 100)
    assert c.EDGE_GRID[-1] == 60.0 and c.LATENCY_NS == 1_000_000
