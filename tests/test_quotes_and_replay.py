"""Quote fill models, and the harness-owned replay a policy trains against.

The trades-only walk is not exercised here. These tests are the new path, and
the regression suite is what says the old one did not move.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from src.core.data.dataset import Calendar, SplitBounds, sessions_for
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.engine.fills import UnfavorableFill, get_fill_model
from src.core.scoring.metrics import IDLE_CHARGE, session_returns
from src.core.scoring.metrics import SessionSpan
from src.core.types import (
    Blotter,
    BlotterRow,
    DataCapabilities,
    Order,
    OrderStatus,
    OrderType,
    Quote,
    Side,
    Split,
    Tick,
    TimeInForce,
)
from src.strategies.rl_reference import RLReference

REPO = Path(__file__).resolve().parents[1]


def _install_replay():
    if "harness_replay" in sys.modules:
        return sys.modules["harness_replay"]
    path = REPO / "src" / "core" / "engine" / "remote" / "replay_env.py"
    spec = importlib.util.spec_from_file_location("harness_replay", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harness_replay"] = module
    spec.loader.exec_module(module)
    return module


class _Flat:
    """Subscribes and does nothing. Enough to open a run."""

    def setup(self, ctx):
        ctx.subscribe("1m")

    def on_bars(self, ctx):
        return None

    def on_fill(self, ctx):
        return None

    def on_lifecycle(self, ctx):
        return None


class _JoinBid:
    def __init__(self):
        self.prices = []

    def setup(self, ctx):
        ctx.subscribe("quote")

    def on_quote(self, ctx):
        if self.prices:
            return None
        ctx.buy(1, type="limit", limit=ctx.quote.bid_price)
        self.prices.append(ctx.quote.bid_price)

    def on_bars(self, ctx):
        return None

    def on_fill(self, ctx):
        self.prices.append(ctx.fill.price)

    def on_lifecycle(self, ctx):
        return None


class _Book:
    """One session, a book we wrote down, and the trades that hit it."""

    name = "book"

    def __init__(self, quotes, ticks):
        self._quotes = list(quotes)
        self._ticks = list(ticks)
        self._cal = Calendar(
            train=SplitBounds(0, 2),
            validation=SplitBounds(3, 4),
            holdout=SplitBounds(5, 6),
            embargo_sessions=1,
            n_sessions=6,
        )

    def capabilities(self):
        return DataCapabilities(has_trades=True, has_quotes=True, has_book=False)

    def calendar(self):
        return self._cal

    def digest(self):
        return "book"

    def session_bounds(self, session):
        return session * 10**12, (session + 1) * 10**12

    def warmup_sessions(self, split, warmup):
        warm, _ = sessions_for(self._cal, split, warmup)
        return set(warm)

    def ticks(self, split, *, warmup=0):
        return iter(self._ticks)

    def quotes(self, split, *, warmup=0):
        return iter(self._quotes)


def _order(limit, side=Side.BUY):
    return Order(
        order_id=1, side=side, size=1, order_type=OrderType.LIMIT,
        tif=TimeInForce.DAY, limit_price=limit, status=OrderStatus.WORKING,
    )


def _quote(ts, bid, bid_size, ask=100.25, ask_size=5):
    return Quote(ts, bid, bid_size, ask, ask_size, session=0)


def _tick(ts, price, size, side):
    return Tick(ts, price, size, session=0, side=side)


def test_a_queue_model_is_refused_on_trades_only_data():
    with pytest.raises(ValueError, match="queue-pessimistic"):
        Engine.for_test(
            SyntheticDataset(SyntheticSpec(
                n_sessions=20, ticks_per_session=5, embargo_sessions=1)),
            _Flat(), fill_model="queue-pessimistic", split=Split.TRAIN,
        )


def test_subscribing_to_quotes_on_the_nq_shaped_adapter_is_refused():
    """The synthetic generator with quotes off has the same capability bit."""
    data = SyntheticDataset(SyntheticSpec(
        n_sessions=20, ticks_per_session=5, embargo_sessions=1))

    class Sub:
        def setup(self, ctx):
            ctx.subscribe("quote")

        def on_bars(self, ctx):
            return None

        def on_quote(self, ctx):
            return None

        def on_fill(self, ctx):
            return None

        def on_lifecycle(self, ctx):
            return None

    with pytest.raises(ValueError, match="no quote"):
        Engine.for_test(data, Sub(), split=Split.TRAIN).run()


def test_quotes_off_leaves_the_digest_and_the_ticks_alone():
    plain = SyntheticSpec(n_sessions=20, ticks_per_session=8, seed=3, embargo_sessions=1)
    quoted = SyntheticSpec(
        n_sessions=20, ticks_per_session=8, seed=3, quotes=True, embargo_sessions=1)
    assert "quotes" not in plain.as_dict()
    a, b = SyntheticDataset(plain), SyntheticDataset(quoted)
    assert a.digest() != b.digest()
    assert list(a.ticks(Split.TRAIN)) == list(b.ticks(Split.TRAIN))
    assert b.capabilities().has_quotes and not a.capabilities().has_quotes


def test_pessimistic_queue_ignores_cancels_and_fills_only_past_them():
    model = get_fill_model("queue-pessimistic")
    order = _order(100.0)
    model.observe(_quote(1, 100.0, 5), [order])
    model.sync(order)
    assert order.queue_ahead == 5
    # Displayed size falls from 5 to 2 with no trade. The queue does not move.
    model.observe(_quote(2, 100.0, 2), [order])
    model.sync(order)
    assert order.queue_ahead == 5
    trade = _tick(3, 100.0, 3, Side.SELL)
    model.note_trade(trade)
    assert model.resolve(order, trade) is None
    assert order.queue_ahead == 2
    trade = _tick(4, 100.0, 3, Side.SELL)
    model.note_trade(trade)
    fill = model.resolve(order, trade)
    assert fill is not None and fill.price == 100.0 and fill.size == 1


def test_prorata_credits_the_cancel_the_pessimistic_model_ignores():
    model = get_fill_model("queue-prorata")
    order = _order(100.0)
    model.observe(_quote(1, 100.0, 10), [order])
    model.sync(order)
    # Size 10 to 5, no trade: half the queue was cancelled.
    model.observe(_quote(2, 100.0, 5), [order])
    model.sync(order)
    assert order.queue_ahead == pytest.approx(5.0)
    trade = _tick(3, 100.0, 6, Side.SELL)
    model.note_trade(trade)
    fill = model.resolve(order, trade)
    assert fill is not None and fill.size == 1 and fill.price == 100.0


def test_a_trade_through_the_limit_fills_without_waiting():
    model = get_fill_model("queue-pessimistic")
    order = _order(100.0)
    model.observe(_quote(1, 100.0, 50), [order])
    model.sync(order)
    trade = _tick(2, 99.75, 1, Side.SELL)
    model.note_trade(trade)
    fill = model.resolve(order, trade)
    assert fill is not None and fill.price == 100.0 and fill.size == 1


def test_the_bar_rule_matches_unfavorable_on_the_low_trade():
    """A one-trade bar's low is a trade price, so the two rules are one rule."""
    order = _order(99.5)
    for low in (99.0, 99.5, 100.0):
        tick = Tick(1, low, 1, session=0, side=Side.SELL)
        engine_fills = UnfavorableFill().resolve(order, tick) is not None
        assert engine_fills == (low < 99.5)


def test_the_engine_joins_the_bid_and_fills_behind_the_displayed_size():
    quotes = [
        _quote(1_000, 100.0, 5),
        _quote(2_000, 100.0, 5),
    ]
    ticks = [
        _tick(3_000, 100.0, 3, Side.SELL),
        _tick(4_000, 100.0, 3, Side.SELL),
    ]
    strategy = _JoinBid()
    record = Engine.for_test(
        _Book(quotes, ticks), strategy, fill_model="queue-pessimistic",
        split=Split.TRAIN,
    ).run()
    assert record.outcome == "completed", record.error
    assert record.fills == 1
    assert strategy.prices == [100.0, 100.0]


def test_replay_reward_matches_session_returns_when_the_cap_is_the_peak():
    replay = _install_replay()
    assert replay.IDLE_CHARGE == IDLE_CHARGE
    from src.core.types import Bar

    def bar(open_ts, close_ts, price, session=0):
        return Bar(
            "time:1", open_ts, close_ts, price, price, price, price,
            1, 1, session,
        )

    bars = [bar(0, 10, 100.0), bar(10, 20, 101.0), bar(20, 30, 103.0)]

    def policy(i, _bar, position):
        if i == 0 and position == 0:
            return ("market", "buy")
        if i == 1 and position > 0:
            return ("market", "sell")
        return None

    env = replay.SessionReplay(commission=0.1, position_cap=1)
    reward = env.run_session(bars, policy)
    blotter = Blotter()
    for i, row in enumerate(env.last_fills):
        blotter.add(BlotterRow(
            seq=i, ts=row["ts"], order_id=i, side=Side(row["side"]),
            price=row["price"], size=row["size"],
            position_after=row["position_after"],
            commission=row["commission"], realized_pnl=row["realized"],
        ))
    session, start, end = env.last_span
    got = session_returns(
        blotter, [SessionSpan(session, start, end)], idle_charge=IDLE_CHARGE,
    )
    assert reward == pytest.approx(got[0])
    # Buy at the second bar's open (101), sell at the third's (103).
    assert env.last_fills[0]["price"] == 101.0
    assert env.last_fills[1]["price"] == 103.0
    assert env.last_fills[1]["realized"] == pytest.approx(2.0)


def test_a_limit_resting_through_a_bar_fills_only_when_the_low_trades_through():
    replay = _install_replay()
    from src.core.types import Bar

    def one(low):
        bar = Bar("t", 0, 10, 100.0, 100.0, low, low, 1, 1, 0)
        env = replay.SessionReplay(commission=0.0, position_cap=1, idle_charge=0.0)

        def policy(i, _bar, _pos):
            return ("limit", "buy", 99.5) if i == 0 else None

        # The decision is eligible on the next bar, so the bar it is tested
        # against has to be a second one.
        nxt = Bar("t", 10, 20, 100.0, 100.0, low, low, 1, 1, 0)
        env.run_session([bar, nxt], policy)
        return env.last_fills

    assert one(99.0) and one(99.0)[0]["price"] == 99.5
    assert one(99.5) == []


def test_the_reference_policy_is_deterministic():
    _install_replay()
    spec = SyntheticSpec(
        n_sessions=20, ticks_per_session=12, seed=2, embargo_sessions=1)

    def once():
        strategy = RLReference()
        record = Engine.for_test(
            SyntheticDataset(spec), strategy, split=Split.TRAIN, fit_sessions=4,
        ).run()
        assert record.outcome == "completed", record.error
        return strategy.weight, strategy.save_weights()

    a, blob_a = once()
    b, blob_b = once()
    assert a == b
    assert blob_a == blob_b
    loaded = RLReference()
    loaded.load_weights(blob_a)
    assert loaded.weight == a
