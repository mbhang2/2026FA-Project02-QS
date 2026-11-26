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
