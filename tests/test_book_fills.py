"""Amendment 20: fills against a real book, pinned on hand-built books."""

from __future__ import annotations

import numpy as np
import pytest
import yaml

import run as runner
from src.core.backtest_config import (BOOK_FILL_MODEL, BacktestConfigError, compile_backtests,
                                      quote_data_problem)
from src.core.data.okx_book import SessionBook, to_lots
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.engine.fills import MAX_QUOTE_AGE_NS, BookQueueFill
from src.core.engine.reconstruct import reconstruct
from src.core.scoring.calibration.reference import ReferenceStrategy
from src.core.scoring.metrics import SessionSpan
from src.core.scoring.randomization import round_turn as rt
from src.core.types import (DataCapabilities, FillRecord, Order, OrderType, Side, Split, Tick,
                            TimeInForce)

MS = 1_000_000


def book(rows, deep=None):
    """rows: (ts_ms, bid, ask, bid_lots, ask_lots)."""
    a = np.array(rows, dtype=float)
    return SessionBook(a[:, 0].astype(np.int64) * MS, a[:, 1], a[:, 2],
                       a[:, 3].astype(np.int64), a[:, 4].astype(np.int64), deep=deep)


def model(b):
    m = BookQueueFill()
    m.bind_book(lambda session: b)
    return m


def order(side=Side.BUY, size=1, kind=OrderType.MARKET, limit=None, stop=None, oid=1,
          arrival_ms=0):
    return Order(order_id=oid, side=side, size=size, order_type=kind, tif=TimeInForce.GTC,
                 limit_price=limit, stop_price=stop, eligible_from_ts=arrival_ms * MS)


def tick(ms, price=100.0, size=1, side=Side.BUY):
    return Tick(ms * MS, price, size, 1, ms, side)


FLAT = [(0, 99.0, 101.0, 5, 7)]


@pytest.mark.parametrize("side, price", [(Side.BUY, 101.0), (Side.SELL, 99.0)])
def test_a_market_order_pays_the_far_side(side, price):
    f = model(book(FLAT)).resolve(order(side=side), tick(10))
    assert (f.price, f.size, f.liquidity, f.mid) == (price, 1, "taker", 100.0)


def test_a_market_order_prices_at_its_arrival_not_at_the_next_trade():
    b = book([(0, 99, 101, 5, 5), (50, 109, 111, 5, 5)])
    assert model(b).resolve(order(arrival_ms=10), tick(60)).price == 101.0


def _deep_book():
    px = np.array([[101.0, 102.0, 103.0, 104.0, 105.0]])
    lots = np.array([[2, 3, 4, 0, 1]])
    return book([(0, 99, 101, 5, 2)], deep=lambda: {
        "ask_px": px, "ask_lots": lots, "bid_px": px - 2, "bid_lots": lots})


def test_a_large_order_walks_the_levels():
    f = model(_deep_book()).resolve(order(size=4), tick(10))
    assert f.size == 4 and f.price == pytest.approx((2 * 101 + 2 * 102) / 4)


def test_an_order_past_five_levels_is_refused_whole():
    m = model(_deep_book())
    assert m.resolve(order(size=11), tick(10)) is None
    assert "five visible" in m.take_rejection(1)
    assert m.take_rejection(1) is None


def test_rows_sharing_a_millisecond_give_the_taker_the_worst():
    b = book([(5, 99, 100, 5, 5), (10, 99, 103, 5, 5), (10, 99, 101, 5, 5)])
    assert model(b).resolve(order(arrival_ms=10), tick(20)).price == 103.0
    s = book([(5, 99, 101, 5, 5), (10, 96, 101, 5, 5), (10, 98, 101, 5, 5)])
    assert model(s).resolve(order(side=Side.SELL, arrival_ms=10), tick(20)).price == 96.0


def test_a_stale_quote_cannot_price_a_taker():
    b = book(FLAT)
    late = MAX_QUOTE_AGE_NS // MS + 1
    assert model(b).resolve(order(arrival_ms=late), tick(late)) is None
    edge = MAX_QUOTE_AGE_NS // MS
    assert model(b).resolve(order(arrival_ms=edge), tick(edge)).price == 101.0


def test_a_stale_arrival_uses_a_fresh_book_at_the_next_trade():
    b = book([(0, 99, 101, 5, 5), (3000, 109, 111, 5, 5)])
    assert model(b).resolve(order(arrival_ms=2000), tick(3001)).price == 111.0


def test_a_triggered_stop_pays_the_book():
    o = order(kind=OrderType.STOP, stop=100.5)
    m = model(book(FLAT))
    assert m.resolve(o, tick(1, price=100.5)) is None           # not strictly through
    assert m.resolve(o, tick(2, price=100.6)).price == 101.0


def test_a_marketable_limit_is_a_taker_and_stops_at_its_limit():
    f = model(_deep_book()).resolve(order(size=9, kind=OrderType.LIMIT, limit=102.0), tick(10))
    assert (f.size, f.liquidity) == (5, "taker")
    assert f.price == pytest.approx((2 * 101 + 3 * 102) / 5)


def _rest(m, o):
    assert m.resolve(o, tick(0, price=100.0, side=Side.BUY)) is None


def test_a_resting_limit_waits_behind_the_displayed_size():
    m = model(book(FLAT))
    o = order(kind=OrderType.LIMIT, limit=99.0, size=3)
    _rest(m, o)
    assert m.resolve(o, tick(1, price=99.0, size=4, side=Side.SELL)) is None
    f = m.resolve(o, tick(2, price=99.0, size=3, side=Side.SELL))
    assert (f.price, f.size, f.liquidity, f.mid) == (99.0, 2, "maker", 100.0)


def test_only_the_aggressor_on_our_side_moves_the_queue():
    m = model(book(FLAT))
    o = order(kind=OrderType.LIMIT, limit=99.0)
    _rest(m, o)
    assert m.resolve(o, tick(1, price=99.0, size=50, side=Side.BUY)) is None
    assert m._resting[1].queue == 5


def test_a_cancel_earns_nothing():
    b = book([(0, 99, 101, 5, 5), (10, 99, 101, 1, 5)])
    m = model(b)
    o = order(kind=OrderType.LIMIT, limit=99.0)
    _rest(m, o)
    assert m.resolve(o, tick(20, price=99.0, size=2, side=Side.SELL)) is None
    assert m._resting[1].queue == 3


def test_a_trade_through_fills_at_the_limit():
    m = model(book(FLAT))
    o = order(kind=OrderType.LIMIT, limit=99.0, size=4)
    _rest(m, o)
    f = m.resolve(o, tick(1, price=98.5, size=3, side=Side.SELL))
    assert (f.price, f.size, f.liquidity) == (99.0, 3, "maker")


def test_leaving_the_level_rejoins_at_the_back():
    b = book([(0, 99, 101, 5, 5), (10, 100, 101, 8, 5), (20, 99, 101, 9, 5)])
    m = model(b)
    o = order(kind=OrderType.LIMIT, limit=99.0)
    _rest(m, o)
    assert m.resolve(o, tick(30, price=99.0, size=2, side=Side.SELL)) is None
    assert m._resting[1].queue == 7


def test_improving_the_best_has_no_queue():
    m = model(book(FLAT))
    o = order(kind=OrderType.LIMIT, limit=100.0)
    _rest(m, o)
    f = m.resolve(o, tick(1, price=100.0, size=1, side=Side.SELL))
    assert (f.size, f.liquidity) == (1, "maker")


def test_a_limit_behind_the_best_has_no_place_until_the_best_reaches_it():
    b = book([(0, 99, 101, 5, 5), (10, 98, 101, 6, 5)])
    m = model(b)
    o = order(kind=OrderType.LIMIT, limit=98.0)
    _rest(m, o)
    assert m._resting[1].queue is None
    assert m.resolve(o, tick(20, price=98.0, size=2, side=Side.SELL)) is None
    assert m._resting[1].queue == 4


def test_the_queue_does_not_depend_on_how_often_it_is_read():
    rows = [(i * 10, 99 + (i % 3 == 1), 101, 5 + i, 5) for i in range(12)]
    every = model(book(rows))
    once = model(book(rows))
    a, b = order(kind=OrderType.LIMIT, limit=99.0), order(kind=OrderType.LIMIT, limit=99.0)
    _rest(every, a), _rest(once, b)
    for i in range(1, 11):
        every._advance(every._resting[1], a, 1, every._book_for(1), i)
    once._advance(once._resting[1], b, 1, once._book_for(1), 10)
    assert every._resting[1].queue == once._resting[1].queue


def _fill(seq, ms, side, price, mid, liquidity):
    return FillRecord(seq=seq, ts=ms * MS, session=0, order_id=seq, side=side, size=1, price=price,
                      liquidity=liquidity, mid=mid)


def test_execution_cost_is_a_cost_and_the_mid_move_is_what_flips():
    fills = [_fill(1, 10, Side.BUY, 101.0, 100.0, "taker"), _fill(2, 20, Side.SELL, 103.0, 104.0, "taker")]
    b = reconstruct(fills, commission_per_contract=0.1)
    cal = [SessionSpan(0, 0, 10**12)]
    [leg] = rt.excursion_legs(b, cal)
    assert b.rows[0].execution_cost == 1.0 and b.rows[1].execution_cost == 1.0
    assert leg.gross == pytest.approx(4.0)            # mid 100 to 104
    assert leg.cost == pytest.approx(0.2 + 2.0)       # fees and both half-spreads
    assert leg.gross - leg.cost == pytest.approx(b.realized_pnl - b.commission)


def test_a_maker_keeps_the_spread_it_earned_when_flipped():
    fills = [_fill(1, 10, Side.BUY, 99.0, 100.0, "maker"), _fill(2, 20, Side.SELL, 101.0, 100.0, "maker")]
    b = reconstruct(fills, commission_per_contract=0.0)
    [leg] = rt.excursion_legs(b, [SessionSpan(0, 0, 10**12)])
    assert leg.gross == pytest.approx(0.0) and leg.cost == pytest.approx(-2.0)


def test_a_record_without_a_mid_hashes_as_it_did():
    f = FillRecord(seq=1, ts=1, session=0, order_id=1, side=Side.BUY, size=1, price=1.0,
                   liquidity="taker")
    assert len(f.as_row()) == 9
    assert reconstruct([f], commission_per_contract=0.0).rows[0].execution_cost == 0.0


def test_lots_must_be_whole():
    assert to_lots(np.array([0.0001, 0.0123]), 0.0001, "x").tolist() == [1, 123]
    with pytest.raises(ValueError, match="whole number"):
        to_lots(np.array([0.00015]), 0.0001, "x")


class SyntheticBook(SyntheticDataset):
    def capabilities(self):
        return DataCapabilities(has_trades=True, has_quotes=False, has_book=True)

    def book_for(self, session):
        t = self._session_ticks(int(session))
        px = np.array([x.price for x in t])
        return SessionBook(np.array([x.ts for x in t], dtype=np.int64), px - 0.25, px + 0.25,
                           np.full(px.size, 3), np.full(px.size, 3))


class Churn:
    """Limits, cancels, modifications and brackets, to exercise every path."""

    def __init__(self):
        self.oid, self.age = None, 0

    def setup(self, ctx):
        ctx.subscribe("5m", emit_empty=True)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position:
            return
        last = ctx.history("5m")[-1]
        if self.oid in ctx.working_order_ids:
            self.age += 1
            if self.age % 3 == 1:
                ctx.modify(self.oid, limit=last.close - 0.25)
            elif self.age % 3 == 2:
                ctx.cancel(self.oid)
            return
        self.oid = ctx.buy(1, type="limit", limit=last.close - 0.25, tif="GTC", stop_offset=0.6,
                           target_offset=0.6)

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass


def _run(strategy, **kw):
    ds = SyntheticBook(SyntheticSpec(n_sessions=40, ticks_per_session=120, vol_per_tick=0.2, seed=5,
                                     uniform=True, tick_size=0.25))
    e = Engine.for_test(ds, strategy, split=Split.TRAIN, warmup_sessions=1, seed=1,
                        fill_model=BOOK_FILL_MODEL, **kw)
    e.run()
    return e


@pytest.mark.parametrize("factory", [ReferenceStrategy, Churn])
@pytest.mark.parametrize("latency", [0, 100_000_000, 45_000_000_000])
@pytest.mark.parametrize("overnight", [True, False])
def test_the_event_walk_matches_stepping_on_a_book(factory, latency, overnight):
    fast = _run(factory(), latency_ns=latency, allow_overnight=overnight, event_walk=True)
    slow = _run(factory(), latency_ns=latency, allow_overnight=overnight, event_walk=False)
    assert slow.blotter.rows
    assert fast.blotter.digest() == slow.blotter.digest()
    assert fast.fill_log_digest() == slow.fill_log_digest()
    assert fast.reconciliation()["agree"]


def test_the_null_identity_holds_on_a_book():
    e = _run(ReferenceStrategy(), latency_ns=100_000_000)
    cal = e.session_calendar()
    blotter = e.reconstructed()
    from src.core.scoring.scorer import score
    observed = score(blotter, period=cal, notional_base=1.0, idle_charge=0.0)
    ident = rt.identity_from_blotter(blotter, cal, idle_charge=0.0, attribution=rt.ATTRIBUTION_FILL)
    assert ident.primary == pytest.approx(observed.primary, abs=1e-9)


def test_a_dataset_without_a_book_is_refused_by_the_engine():
    ds = SyntheticDataset(SyntheticSpec(n_sessions=40, ticks_per_session=120, seed=1))
    with pytest.raises(Exception):
        Engine.for_test(ds, ReferenceStrategy(), split=Split.TRAIN, fill_model=BOOK_FILL_MODEL)


def _cfg():
    return {"profiles": {"old": {}}, "datasets": {"quoted": {"book": True}, "plain": {}}}


def _write(tmp_path, **entry):
    p = tmp_path / "backtest.yaml"
    p.write_text(yaml.safe_dump({"backtests": {"b": {"dataset": "quoted", "split": "train",
                                                     "use_quotes": True, "costs": "BTCUSDT",
                                                     **entry}}}))
    return p


def test_quotes_on_a_book_dataset_compile():
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        out = compile_backtests(_cfg(), _write(pathlib.Path(d)))
    assert out["profiles"]["b"]["fill_model"] == BOOK_FILL_MODEL


@pytest.mark.parametrize("entry, message", [
    ({"dataset": "plain"}, "no quotes"),
    ({"fill_model": "unfavorable"}, "quotes need"),
    ({"use_quotes": False, "fill_model": BOOK_FILL_MODEL}, "needs use_quotes"),
    ({"use_quotes": "yes"}, "true or false"),
    ({"typo": 1}, "unknown keys"),
    ({"dataset": "missing"}, "no dataset"),
])
def test_a_bad_backtest_is_refused(tmp_path, entry, message):
    with pytest.raises(BacktestConfigError, match=message):
        compile_backtests(_cfg(), _write(tmp_path, **entry))


def test_a_backtest_cannot_replace_a_profile(tmp_path):
    p = _write(tmp_path)
    p.write_text(p.read_text().replace("  b:", "  old:"))
    with pytest.raises(BacktestConfigError, match="existing profile"):
        compile_backtests(_cfg(), p)


def test_quotes_without_quote_data_do_not_run():
    plain = SyntheticDataset(SyntheticSpec(n_sessions=40, ticks_per_session=120, seed=1))
    assert "no quote data" in quote_data_problem(
        {"use_quotes": True, "fill_model": BOOK_FILL_MODEL}, plain, Split.TRAIN, 0)
    assert quote_data_problem({"use_quotes": False}, plain, Split.TRAIN, 0) is None


def test_the_registered_backtests():
    cfg = runner.load_config()
    for name, split in (("btc-okx-book-train", "train"), ("btc-okx-book-validation", "validation")):
        p = cfg["profiles"][name]
        assert (p["use_quotes"], p["fill_model"], p["split"]) == (True, BOOK_FILL_MODEL, split)
        assert (p["commission_bps"], p["maker_commission_bps"], p["latency_ns"]) == (5.0, 5.0, 100_000_000)


def test_a_quoted_backtest_is_refused_until_signed(monkeypatch):
    import prereg

    monkeypatch.setattr(prereg, "in_force", lambda n, text=None: False)
    with pytest.raises(SystemExit, match="Amendment 20"):
        runner.main(["--human", "--purpose", "check", "--profile", "btc-okx-book-train"])
