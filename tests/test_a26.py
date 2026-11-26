"""Amendment 26: market-making returns with inventory marked, and its control's definition."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from src.core.engine import native_trial as T  # noqa: E402


def test_only_marked_returns_are_read():
    assert T.marked_returns({"session_returns": [1.0], "inventory_marked": True}) == [1.0]
    assert T.marked_returns({"session_returns": [1.0]}) is None
    assert T.marked_returns({"session_returns": [1.0], "inventory_marked": "yes"}) is None
    assert T.marked_returns({"inventory_marked": True}) is None


def test_the_holdout_does_not_confirm_unmarked_returns():
    import holdout

    assert holdout.judge_spread({"session_returns": [1.0] * 200})["confirmed"] is False


def test_stage_nulls_refuses_unmarked_market_making_rows():
    text = (ROOT / "scripts" / "nq_campaign.py").read_text()
    assert "Amendment 26 F4 refuses them for gate spread_capture" in text
    assert "returns = marked_returns(row)" in text


def test_the_control_uses_new_seeds_and_a25s_alpha_and_conditions():
    import crypto_mm_control as M

    seeds = M.A26_UNPROFITABLE_SEEDS + M.A26_NOISE_SEEDS + M.A26_STRONG_SEEDS
    used = set(range(620_001, 622_501)) | set(range(630_001, 632_501)) | set(range(640_001, 642_501))
    used |= set(M.NOISE_RERUN_SEEDS) | set(M.CALIBRATION_SEEDS)
    used |= set(M.A25_UNPROFITABLE_SEEDS + M.A25_NOISE_SEEDS + M.A25_STRONG_SEEDS)
    assert used.isdisjoint(seeds) and used.isdisjoint(M.A26_CALIBRATION_SEEDS)
    assert set(seeds).isdisjoint(M.A26_CALIBRATION_SEEDS)
    assert len(set(seeds)) == len(seeds) == 2100 and len(M.A26_CALIBRATION_SEEDS) == 100
    assert M.A26_CALIBRATION_SEEDS[0] == 670_001 and M.A26_STRONG_SEEDS[-1] == 673_100
    assert M.A26_ALPHA == pytest.approx(0.005)
    counts = {"unprofitable": 1000, "noise": 1000, "strong": 100}
    assert M.a26_verdict({"unprofitable": 36, "noise": 36, "strong": 80}, counts)["accepted"] is True
    assert M.a26_verdict({"unprofitable": 37, "noise": 0, "strong": 100}, counts)["accepted"] is False
    assert M.a26_verdict({"unprofitable": 0, "noise": 0, "strong": 79}, counts)["accepted"] is False
    with pytest.raises(ValueError):
        M.a26_verdict({"unprofitable": 0, "noise": 0, "strong": 100}, {**counts, "noise": 400})


def test_the_amendment_is_a_draft_and_the_gate_is_off():
    import prereg

    assert prereg.in_force(25) and not prereg.in_force(26)


@pytest.fixture(scope="module")
def variant():
    pytest.importorskip("numba")
    import crypto_mm_control as M
    from src.core.engine import native

    try:
        native.load()
    except Exception as exc:  # pragma: no cover - no native library here
        pytest.skip(f"native library unavailable: {exc}")
    cfg = M.gate.load_config()
    profile = cfg["profiles"][M.PROFILE]
    dataset = M.MarketMakingDataset(M.spec_for(9301), 0.57)
    columns = {}
    blotter, calendar, terms, count = M.run_variant(dataset, 0.5, cfg, profile, columns_out=columns)
    return M, cfg, profile, dataset, columns, blotter, calendar, terms


def test_the_controls_unmarked_columns_score_equals_the_blotter_score(variant):
    M, cfg, profile, dataset, columns, blotter, calendar, terms = variant
    from src.core.engine import column_score

    costs = {"commission_per_contract": profile["commission_per_contract"],
             "commission_bps": profile["commission_bps"], "maker_commission_bps": profile["maker_commission_bps"]}
    plain, returns = column_score.score_columns(columns, costs=costs, period=calendar, **terms)
    assert plain == M.gate.score(blotter, period=calendar, **terms)
    assert returns == M.gate.session_returns(blotter, calendar, **terms)


def test_the_controls_marks_add_the_final_open_value(variant):
    M, cfg, profile, dataset, columns, blotter, calendar, terms = variant
    from src.core.engine import column_score
    from src.core.scoring import metrics

    _, marked, _ = M.marked_variant(dataset, 0.5, cfg, profile)
    unmarked = M.gate.session_returns(blotter, calendar, **terms)
    costs = {"commission_per_contract": profile["commission_per_contract"],
             "commission_bps": profile["commission_bps"], "maker_commission_bps": profile["maker_commission_bps"]}
    b = column_score.blotter_columns(columns, **costs)
    closes = M.closes_for(dataset, calendar)
    marks = column_score.inventory_marks(b, calendar, closes)
    for m, u, mk, span in zip(marked, unmarked, marks, calendar):
        assert math.isclose(m - u, mk / metrics.per_session(terms["notional_base"], span.session),
                            rel_tol=1e-9, abs_tol=1e-12)
    last = int(np.lexsort((b["seq"], b["ts"]))[-1])
    q = int(b["position_after"][last])
    final = q * (closes[calendar[-1].session] - float(b["avg_after"][last])) if q else 0.0
    assert q == blotter.rows[-1].position_after
    assert math.isclose(math.fsum(marks), final, rel_tol=1e-9, abs_tol=1e-9)
    assert any(mk != 0 for mk in marks)
