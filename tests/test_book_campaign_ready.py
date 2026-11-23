"""A book-aware campaign can be told about the book, probed and read quickly."""

from __future__ import annotations

import numpy as np
import pytest
import yaml

from pathlib import Path

from src.core.backtest_config import prepare_config
from src.core.data.okx_book import SessionBook, encode_depth
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.loop import market, perturb, probe
from src.core.types import Side

ROOT = Path(__file__).resolve().parents[1]


def config():
    return prepare_config(yaml.safe_load((ROOT / "config" / "run.yaml").read_text()))


def test_the_hft_profile_gets_book_facts():
    key = market.market_for_profile("btc-okx-hft-train", config())
    assert key == "crypto-okx-book-BTCUSDT"
    facts = market.facts(key)
    assert "ctx.book()" in facts["data"] and "0 basis points" in facts["data"]
    assert "2 bp" in facts["have"] and "cannot read quotes" not in facts["have"]


def test_the_old_okx_facts_are_unchanged_in_meaning():
    key = market.market_for_profile("btc-okx-book-train", config())
    assert key == "crypto-okx-BTCUSDT"
    assert "cannot read quotes" in market.facts(key)["have"]


def test_book_campaigns_use_book_probes():
    source = "class S:\n    def on_bars(self, ctx):\n        pass\n"
    key = "crypto-okx-book-BTCUSDT"
    assert probe.probe_profile_for(source, key) == "agent-crypto-book"
    assert perturb.PAIRS["crypto-book"] == ("probe-crypto-book", "probe-crypto-book-flipped")
    cfg = config()
    for name in ("agent-crypto-book", "harness-fitted-crypto-book", "probe-crypto-book",
                 "probe-crypto-book-flipped"):
        prof = cfg["profiles"][name]
        spec = cfg["datasets"][prof["dataset"]]
        assert prof["book_features"] is True
        assert spec["kind"] == "synthetic" and spec["book"] is True
        assert float(spec["signal_strength"]) == 0.0


def test_a_synthetic_book_is_absent_from_old_digests():
    assert "book" not in SyntheticSpec().as_dict()
    assert SyntheticSpec(book=True).as_dict()["book"] is True


def test_a_synthetic_book_has_five_levels_and_no_later_price():
    spec = SyntheticSpec(n_sessions=40, ticks_per_session=60, book=True, seed=3)
    first = SyntheticDataset(spec)
    session = 3
    book = first.book_for(session)
    prices, lots = book.levels(5, Side.BUY)
    assert len(prices) == 5 and len(lots) == 5
    assert np.all(np.diff(prices) > 0)
    ticks = first._session_ticks(session)
    assert book.ts[0] == ticks[0].ts
    assert book.ask[1] == pytest.approx(ticks[0].price + spec.tick_size)
    assert book.ts[1] == ticks[1].ts - 1_000_000


def test_depth_is_stored_exactly_or_refused():
    prices = np.array([[100.1, 100.2], [99999.9, 111987.5]])
    lots = np.array([[1, 2], [5_955_200, 0]])
    px, qty = encode_depth(prices, lots, 10)
    assert px.dtype == np.int32 and qty.dtype == np.int32
    assert np.array_equal(px / 10, prices) and np.array_equal(qty, lots)
    with pytest.raises(ValueError):
        encode_depth(np.array([[100.15]]), np.array([[1]]), 10)
    with pytest.raises(ValueError):
        encode_depth(np.array([[1.0]]), np.array([[2**31]]), 10)


def test_stored_depth_reads_like_decoded_depth():
    bid_px = np.array([[100.0, 99.9, 99.8, 99.7, 99.6], [100.1, 100.0, 99.9, 99.8, 99.7]])
    ask_px = bid_px + 1.0
    bid_lots = np.array([[1, 2, 3, 4, 5], [6, 7, 8, 9, 0]])
    ask_lots = bid_lots + 10
    deep = {"bid_px": bid_px, "ask_px": ask_px, "bid_lots": bid_lots, "ask_lots": ask_lots}
    stored = {"scale": 10}
    for side in ("bid", "ask"):
        stored[f"{side}_px"], stored[f"{side}_lots"] = encode_depth(
            deep[f"{side}_px"][:, 1:], deep[f"{side}_lots"][:, 1:], 10)
    ts = np.array([1, 2], dtype=np.int64)
    old = SessionBook(ts, bid_px[:, 0], ask_px[:, 0], bid_lots[:, 0], ask_lots[:, 0], deep=lambda: deep)
    new = SessionBook(ts, bid_px[:, 0], ask_px[:, 0], bid_lots[:, 0], ask_lots[:, 0], depth=stored)
    for row in (0, 1):
        for side in (Side.BUY, Side.SELL):
            a, b = old.levels(row, side), new.levels(row, side)
            assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])


def test_an_empty_level_survives_the_depth_store():
    from src.core.data.okx_book import decode_depth

    prices = np.array([[100.1, np.nan], [np.nan, np.nan]])
    lots = np.array([[3, 0], [0, 0]])
    px, qty = encode_depth(prices, lots, 10)
    assert np.array_equal(decode_depth(px, 10), prices, equal_nan=True)
    assert np.array_equal(qty, lots)
    with pytest.raises(ValueError):
        encode_depth(np.array([[np.nan]]), np.array([[1]]), 10)
