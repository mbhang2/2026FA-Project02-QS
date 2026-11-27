"""Scoring from fill columns equals scoring the reconstructed blotter, exactly."""

from __future__ import annotations

import json

import numpy as np
import pytest

from src.core.engine import column_score as C
from src.core.engine.native_trial import records_from_columns
from src.core.engine.reconstruct import reconstruct
from src.core.scoring import metrics
from src.core.scoring.scorer import score

DAY = 86_400_000_000_000


def _calendar(n):
    return [metrics.SessionSpan(1000 + i, i * DAY, (i + 1) * DAY - 1) for i in range(n)]


def _fills(rng, n_sessions, n):
    ts = np.sort(rng.integers(0, n_sessions * DAY, n)).astype(np.int64)
    seq = np.cumsum(rng.integers(0, 3, n)).astype(np.int64)            # ties share a sequence number
    return {"seq": seq, "ts": ts, "session": (ts // DAY + 1000).astype(np.int64),
            "order_id": np.arange(1, n + 1, dtype=np.int64)[rng.permutation(n)],
            "side": rng.choice(np.array([-1, 1], np.int8), n), "size": rng.integers(1, 40, n).astype(np.int64),
            "price": np.round(30000 + np.cumsum(rng.normal(0, 3, n)), 1), "maker": rng.integers(0, 2, n).astype(np.int8),
            "mid": np.round(30000 + rng.normal(0, 1, n), 2)}


COSTS = [
    dict(commission_per_contract=0.0, commission_bps=2.0, maker_commission_bps=0.0),
    dict(commission_per_contract=0.01, commission_bps=0.0),
    dict(commission_per_contract=0.0, commission_bps=2.0, maker_commission_bps=0.0, slippage_bps=1.5,
         slippage_per_contract=0.02),
]


def _funding(rng, cal):
    return [metrics.FundingEvent(ts=int(s.start_ts + h * 3_600_000_000_000), rate=float(rng.normal(1e-4, 5e-5)),
                                 mark=30000.0) for s in cal for h in (0, 8, 16)]


@pytest.mark.parametrize("seed", range(12))
def test_column_scoring_equals_the_blotter(seed):
    rng = np.random.default_rng(seed)
    n_sessions = int(rng.integers(30, 70))
    cal = _calendar(n_sessions)
    cols = _fills(rng, n_sessions, int(rng.integers(50, 3000)))
    costs = COSTS[seed % len(COSTS)]
    kw = dict(notional_base=30000.0, idle_charge=[0.0, 0.18, 1.2e-5 * 30000][seed % 3],
              funding=_funding(rng, cal) if seed % 2 else None)
    floor = None if seed % 4 else 0.3
    blotter = reconstruct(records_from_columns(cols), **costs)
    expected = score(blotter, period=cal, min_exposed_fraction=floor, **kw)
    got, returns = C.score_columns(cols, costs=costs, period=cal, min_exposed_fraction=floor, **kw)
    assert json.dumps(got.as_dict(), sort_keys=True) == json.dumps(expected.as_dict(), sort_keys=True)
    assert returns == metrics.session_returns(blotter, cal, **kw)


def test_the_columns_match_the_rows():
    rng = np.random.default_rng(99)
    cols = _fills(rng, 20, 2000)
    blotter = reconstruct(records_from_columns(cols), **COSTS[0])
    b = C.blotter_columns(cols, **COSTS[0])
    assert b["position_after"].tolist() == [r.position_after for r in blotter.rows]
    assert b["realized"].tolist() == [r.realized_pnl for r in blotter.rows]
    assert b["commission"].tolist() == [r.commission for r in blotter.rows]


def test_a_position_held_past_the_end_is_charged_to_the_end():
    cal = _calendar(5)
    cols = {k: np.array(v) for k, v in dict(seq=[0], ts=[DAY + 5], session=[1001], order_id=[1], side=[1],
                                            size=[3], price=[100.0], maker=[1], mid=[100.0]).items()}
    cols = {k: v.astype(np.int8 if k in ("side", "maker") else (np.float64 if k in ("price", "mid") else np.int64))
            for k, v in cols.items()}
    blotter = reconstruct(records_from_columns(cols), **COSTS[0])
    kw = dict(notional_base=100.0, idle_charge=0.18, funding=None)
    assert C.score_columns(cols, costs=COSTS[0], period=cal, **kw)[1] == metrics.session_returns(blotter, cal, **kw)


def test_the_digest_follows_the_columns():
    rng = np.random.default_rng(3)
    cols = _fills(rng, 5, 100)
    a = C.columns_digest(cols)
    cols["price"][7] += 0.1
    assert C.columns_digest(cols) != a


def test_sorted_and_unsorted_columns_give_the_same_score():
    rng = np.random.default_rng(11)
    cal = _calendar(40)
    cols = _fills(rng, 40, 2500)
    order = np.lexsort((cols["order_id"], cols["seq"]))
    ordered = {k: v[order] for k, v in cols.items()}
    kw = dict(notional_base=30000.0, idle_charge=0.18, funding=_funding(rng, cal))
    a = C.score_columns(cols, costs=COSTS[2], period=cal, **kw)
    b = C.score_columns(ordered, costs=COSTS[2], period=cal, **kw)
    assert a[1] == b[1] and a[0].as_dict() == b[0].as_dict()
    assert C._sequence_order(ordered) is None and C._sequence_order(cols) is not None
    assert C.columns_digest(cols) == C.columns_digest(ordered)


def _closes(cal, rng):
    return {s.session: float(30000 + rng.normal(0, 50)) for s in cal}


def test_inventory_marks_add_up_to_the_final_open_value():
    rng = np.random.default_rng(21)
    cal = _calendar(30)
    cols = _fills(rng, 30, 1500)
    closes = _closes(cal, rng)
    b = C.blotter_columns(cols, **COSTS[0])
    marks = C.inventory_marks(b, cal, closes)
    last = int(np.lexsort((b["seq"], b["ts"]))[-1])
    final = int(b["position_after"][last]) * (closes[cal[-1].session] - float(b["avg_after"][last]))
    assert abs(sum(marks) - final) < 1e-6 * max(1.0, abs(final))


def test_a_position_flat_at_every_close_is_not_moved_by_marks():
    rng = np.random.default_rng(5)
    cal = _calendar(10)
    rows = []
    for i, s in enumerate(cal):
        for k, (side, ts) in enumerate(((1, s.start_ts + 10), (-1, s.start_ts + 20))):
            rows.append((2 * i + k, ts, s.session, 2 * i + k + 1, side, 3, 100.0 + i + k, 1, 100.0))
    cols = {name: np.array([r[j] for r in rows], dtype)
            for j, (name, dtype) in enumerate((("seq", np.int64), ("ts", np.int64), ("session", np.int64),
                                               ("order_id", np.int64), ("side", np.int8), ("size", np.int64),
                                               ("price", np.float64), ("maker", np.int8), ("mid", np.float64)))}
    kw = dict(notional_base=100.0, idle_charge=0.18, funding=None)
    plain = C.score_columns(cols, costs=COSTS[0], period=cal, **kw)
    marked = C.score_columns(cols, costs=COSTS[0], period=cal, closes=_closes(cal, rng), **kw)
    assert plain[1] == marked[1]


def test_holding_a_short_for_funding_through_a_rally_now_loses():
    """btc-mm-wide-01 trial 4: short 600 lots at 17,270, never closed, while funding is collected."""
    cal = _calendar(60)
    cols = {"seq": np.array([0], np.int64), "ts": np.array([cal[0].start_ts + 5], np.int64),
            "session": np.array([cal[0].session], np.int64), "order_id": np.array([1], np.int64),
            "side": np.array([-1], np.int8), "size": np.array([600], np.int64), "price": np.array([17270.0]),
            "maker": np.array([1], np.int8), "mid": np.array([17270.0])}
    closes = {s.session: 17270.0 + 1500.0 * (i + 1) for i, s in enumerate(cal)}
    funding = [metrics.FundingEvent(ts=int(s.start_ts + h * 3_600_000_000_000), rate=1e-4, mark=closes[s.session])
               for s in cal for h in (0, 8, 16)]
    kw = dict(notional_base=17270.0 * 600, idle_charge=0.0, funding=funding)
    unmarked = C.score_columns(cols, costs=COSTS[0], period=cal, **kw)[1]
    marked = C.score_columns(cols, costs=COSTS[0], period=cal, closes=closes, **kw)[1]
    assert sum(unmarked) > 0 > sum(marked)


def test_an_open_position_without_a_close_price_is_refused():
    cal = _calendar(3)
    cols = {"seq": np.array([0], np.int64), "ts": np.array([cal[0].start_ts + 5], np.int64),
            "session": np.array([cal[0].session], np.int64), "order_id": np.array([1], np.int64),
            "side": np.array([1], np.int8), "size": np.array([2], np.int64), "price": np.array([100.0]),
            "maker": np.array([1], np.int8), "mid": np.array([100.0])}
    with pytest.raises(ValueError, match="no close price"):
        C.score_columns(cols, costs=COSTS[0], period=cal, closes={}, notional_base=100.0, idle_charge=0.0)
