"""Amendment 24's market-making control: its market, kernels and setting rules."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import crypto_mm_control as M  # noqa: E402

from src.core.engine import kernel as K  # noqa: E402


@pytest.mark.parametrize("skew", M.SKEWS)
def test_every_control_kernel_is_in_the_kernel_language(skew):
    K.check(M.kernel_source(skew))


def test_the_skews_set_distinct_inventory_limits():
    limits = [M.limit_for(s) for s in M.SKEWS]
    assert limits == sorted(limits, reverse=True) and len(set(limits)) == len(limits)
    assert limits[0] == M.CAP_LOTS and limits[-1] == 1


def test_the_market_is_deterministic_and_its_book_shows_only_the_past():
    a = M.MarketMakingDataset(M.spec_for(9301), 0.5)
    b = M.MarketMakingDataset(M.spec_for(9301), 0.5)
    session = a.scored_sessions(M.Split.TRAIN, 0)[3]
    for x, y in zip(a.trades(session), b.trades(session)):
        assert np.array_equal(x, y)
    ts, price, size, side = a.trades(session)
    book = a.book_for(session)
    # Each trade prints at the touch of the row before it, and each row comes from trades up to its time.
    row = np.searchsorted(book.ts, ts, side="left") - 1
    assert (row >= 0).all()
    touch = np.where(side > 0, np.asarray(book.ask)[row], np.asarray(book.bid)[row])
    assert np.allclose(touch, price)
    assert (np.asarray(book.ts)[1:] == ts).all()


def test_the_next_session_opens_where_the_last_closed():
    data = M.MarketMakingDataset(M.spec_for(9302), 1.0)
    sessions = data.scored_sessions(M.Split.TRAIN, 0)[:2]
    last = data._path(sessions[0])["mid_after"][-1]
    assert sessions[1] == sessions[0] + 1
    assert data._path(sessions[1])["row_mid"][0] == last


def _rows(table):
    rows = []
    for impact, (mean, sharpe) in table.items():
        for seed in range(4):
            rows.append({"impact": impact, "seed": seed, "means": [mean + 1e-6 * seed] * 5,
                         "sharpes": [sharpe] * 5})
    return rows


def test_settings_are_chosen_and_ordered():
    chosen = M.select_settings(_rows({0.0: (4e-4, 0.5), 0.3: (2e-4, 0.2), 0.6: (-1e-6, 0.0),
                                      0.9: (-3e-4, -0.3)}))
    assert (chosen["strong_impact"], chosen["noise_impact"], chosen["unprofitable_impact"]) == (0.0, 0.6, 0.9)


def test_a_calibration_without_a_strong_setting_fails():
    chosen = M.select_settings(_rows({0.0: (4e-4, 0.2), 0.6: (-1e-6, 0.0), 0.9: (-3e-4, -0.3)}))
    assert chosen["strong_impact"] is None and chosen["noise_impact"] is None


@pytest.mark.skipif(importlib.util.find_spec("numba") is None, reason="numba is not installed")
def test_one_variant_runs_and_respects_the_cap():
    data = M.MarketMakingDataset(M.spec_for(9301), 0.3)
    cfg = M.gate.load_config()
    blotter, calendar, terms, fills = M.run_variant(data, 0.5, cfg, cfg["profiles"][M.PROFILE])
    assert fills > 1000 and len(calendar) > 30
    assert max(abs(r.position_after) for r in blotter.rows) <= M.limit_for(0.5)


def test_the_grid_is_fine_where_returns_cross_zero():
    fine = [i for i in M.IMPACT_GRID if 0.4 <= i <= 0.8]
    assert len(fine) == 21 and all(abs(b - a - 0.02) < 1e-9 for a, b in zip(fine, fine[1:]))
    assert list(M.IMPACT_GRID) == sorted(set(M.IMPACT_GRID))


def test_noise_is_chosen_strictly_below_the_unprofitable_setting():
    """The first calibration's failure: the point closest to zero was also the unprofitable one."""
    chosen = M.select_settings(_rows({0.0: (4e-4, 0.5), 0.5: (4.6e-5, 0.1), 0.6: (-2.6e-5, -0.03),
                                      0.7: (-9e-5, -0.1)}))
    # 0.6 is both closest to zero and the smallest all-negative point if its bounds are negative,
    # so noise must come from below it.
    assert chosen["noise_impact"] is not None and chosen["noise_impact"] < chosen["unprofitable_impact"]


def test_the_revised_market_trades_every_ten_seconds():
    spec = M.spec_for(9301)
    assert spec.tick_spacing_ns == 10_000_000_000 and spec.ticks_per_session == 8640
    assert set(M.CALIBRATION_SEEDS).isdisjoint(range(620_001, 622_501))
