"""Amendment 25: every market-making test at half the error, and its control's definition."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from src.core.engine import native_trial as T  # noqa: E402


def test_the_factor_halves_the_error():
    assert T.SPREAD_CAPTURE_ALPHA_FACTOR == 0.5


def test_the_holdout_bound_is_taken_at_half_q(monkeypatch):
    import holdout
    import src.core.scoring.profitability as P

    seen = []
    monkeypatch.setattr(P, "profitability_bound", lambda returns, alpha: seen.append(alpha) or {"passes": True})
    holdout.judge_spread({"session_returns": [1e-3] * 50, "inventory_marked": True})
    assert seen == [pytest.approx(holdout.Q / 2)]


def test_the_control_uses_new_seeds_half_the_alpha_and_a_thousand_noise_campaigns():
    import crypto_mm_control as M

    seeds = M.A25_UNPROFITABLE_SEEDS + M.A25_NOISE_SEEDS + M.A25_STRONG_SEEDS
    used = set(range(620_001, 622_501)) | set(range(630_001, 632_501)) | set(range(640_001, 642_501))
    used |= set(M.NOISE_RERUN_SEEDS)
    assert used.isdisjoint(seeds) and len(set(seeds)) == len(seeds) == 2100
    assert len(M.A25_NOISE_SEEDS) == 1000 and M.A25_ALPHA == pytest.approx(0.005)
    counts = {"unprofitable": 1000, "noise": 1000, "strong": 100}
    assert M.a25_verdict({"unprofitable": 36, "noise": 36, "strong": 80}, counts)["accepted"] is True
    assert M.a25_verdict({"unprofitable": 36, "noise": 37, "strong": 100}, counts)["accepted"] is False
    assert M.a25_verdict({"unprofitable": 0, "noise": 0, "strong": 79}, counts)["accepted"] is False


def test_the_amendment_is_in_force():
    import prereg

    assert prereg.in_force(25) is True
