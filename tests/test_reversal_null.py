"""Amendment 21: a reversal ends a crypto round turn."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.core.scoring import metrics
from src.core.scoring.randomization import round_turn as rt
from src.core.types import Blotter, BlotterRow, Side

ROOT = Path(__file__).resolve().parents[1]
DAY = 86_400_000_000_000
CALENDAR = [metrics.SessionSpan(i, i * DAY, (i + 1) * DAY - 1) for i in range(40)]


def row(seq, day, side, size, after, realized=0.0, commission=1.0, execution=0.0):
    return BlotterRow(seq=seq, ts=day * DAY + seq, order_id=seq, side=side, size=size,
                      price=100.0, commission=commission, position_after=after,
                      realized_pnl=realized, execution_cost=execution)


def blotter(*rows):
    return Blotter(rows=list(rows))


def test_a_direct_reversal_splits_units_and_shares_costs():
    b = blotter(row(1, 0, Side.BUY, 1, 1, commission=2.0, execution=0.2),
                row(2, 1, Side.SELL, 3, -2, realized=9.0, commission=6.0, execution=0.6),
                row(3, 2, Side.BUY, 2, 0, realized=-4.0, commission=4.0, execution=0.4))
    legs = rt.excursion_legs(b, CALENDAR, rt.UNIT_ZERO_CROSSING)
    assert sorted({x.excursion for x in legs}) == [0, 1]
    old = [x for x in legs if x.excursion == 0]
    new = [x for x in legs if x.excursion == 1]
    assert sum(x.gross for x in old) == pytest.approx(9.0 + 0.2 + 0.2)
    assert sum(x.cost for x in old) == pytest.approx(2.2 + 6.6 / 3)
    assert sum(x.cost for x in new) == pytest.approx(4.4 + 6.6 * 2 / 3)
    assert rt.risk_units(b, CALENDAR) == 2
    assert rt.risk_units(b, CALENDAR, rt.UNIT_FLAT) == 1


def test_net_pnl_is_conserved_across_a_crossing():
    b = blotter(row(1, 0, Side.BUY, 2, 2, execution=0.3),
                row(2, 0, Side.SELL, 5, -3, realized=7.0, commission=5.0, execution=-0.5),
                row(3, 1, Side.BUY, 3, 0, realized=2.0, execution=0.1))
    flat = sum(x.gross - x.cost for x in rt.excursion_legs(b, CALENDAR, rt.UNIT_FLAT))
    crossing = sum(x.gross - x.cost for x in rt.excursion_legs(b, CALENDAR, rt.UNIT_ZERO_CROSSING))
    assert crossing == pytest.approx(flat, abs=1e-12)


def test_partial_closes_and_scale_ins_do_not_end_a_unit():
    b = blotter(row(1, 0, Side.BUY, 1, 1), row(2, 0, Side.BUY, 1, 2),
                row(3, 1, Side.SELL, 1, 1, realized=1.0), row(4, 2, Side.SELL, 1, 0, realized=1.0))
    assert rt.risk_units(b, CALENDAR) == 1


def test_a_reversal_across_midnight_keeps_each_part_in_its_session():
    b = blotter(row(1, 0, Side.BUY, 1, 1), row(2, 1, Side.SELL, 2, -1, realized=3.0),
                row(3, 2, Side.BUY, 1, 0, realized=1.0))
    legs = rt.excursion_legs(b, CALENDAR, rt.UNIT_ZERO_CROSSING)
    assert {(x.excursion, x.session) for x in legs} == {(0, 0), (0, 1), (1, 1), (1, 2)}


def test_an_open_final_unit_is_included():
    b = blotter(row(1, 0, Side.BUY, 1, 1), row(2, 1, Side.SELL, 2, -1, realized=1.0))
    assert rt.risk_units(b, CALENDAR) == 2


def test_no_flip_draw_equals_the_scored_returns():
    rows = [row(1, 0, Side.BUY, 1, 1)]
    position = 1
    for i in range(2, 22):
        rows.append(row(i, i, Side.SELL if position > 0 else Side.BUY, 2, -position,
                        realized=(-1) ** i * 2.5, execution=0.1))
        position = -position
    b = blotter(*rows)
    direct = rt.identity_from_legs(rt.excursion_legs(b, CALENDAR, rt.UNIT_FLAT),
                                   [c.session for c in CALENDAR])
    crossing = rt.identity_from_legs(rt.excursion_legs(b, CALENDAR, rt.UNIT_ZERO_CROSSING),
                                     [c.session for c in CALENDAR])
    assert crossing.primary == pytest.approx(direct.primary, abs=1e-9)


def test_a_strategy_that_never_crosses_has_the_same_null_draws():
    rows = []
    for i in range(1, 25, 2):
        rows += [row(i, i, Side.BUY, 1, 1), row(i + 1, i, Side.SELL, 1, 0, realized=(-1) ** i * 2.0)]
    b = blotter(*rows)
    kwargs = {"b": 500, "seed": 3, "attribution": rt.ATTRIBUTION_FILL}
    assert rt.null_sample(b, CALENDAR, unit=rt.UNIT_FLAT, **kwargs) == rt.null_sample(
        b, CALENDAR, unit=rt.UNIT_ZERO_CROSSING, **kwargs)


def test_zero_crossing_needs_fill_attribution():
    b = blotter(row(1, 0, Side.BUY, 1, 1), row(2, 1, Side.SELL, 1, 0))
    with pytest.raises(ValueError):
        rt.null_sample(b, CALENDAR, b=10, unit=rt.UNIT_ZERO_CROSSING)


def test_holding_periods_end_at_a_reversal():
    b = blotter(row(1, 0, Side.BUY, 1, 1), row(2, 2, Side.SELL, 2, -1), row(3, 5, Side.BUY, 1, 0))
    holds = metrics._risk_unit_holding_periods(b.rows)
    assert holds == [2 * DAY + 1, 3 * DAY + 1]
    assert len(metrics._holding_periods(b.rows)) == 1


def _control():
    spec = importlib.util.spec_from_file_location("crypto_reversal_control",
                                                  ROOT / "scripts" / "crypto_reversal_control.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_control_constants_are_the_registered_ones():
    c = _control()
    assert c.CALIBRATION_SEEDS[0] == 500_001 and len(c.CALIBRATION_SEEDS) == 100
    assert (c.UNPROFITABLE_SEEDS[0], len(c.UNPROFITABLE_SEEDS)) == (501_001, 1_000)
    assert (c.NOISE_SEEDS[0], len(c.NOISE_SEEDS)) == (502_001, 400)
    assert (c.STRONG_SEEDS[0], len(c.STRONG_SEEDS)) == (502_401, 100)
    assert c.EDGE_GRID[0] == 0.0 and c.EDGE_GRID[-1] == 60.0 and len(c.EDGE_GRID) == 31
    assert c.HOLD_HOURS == (1, 2, 4, 8, 12)
    assert rt.MIN_RISK_UNITS == 30


def test_the_control_picks_the_registered_edges():
    c = _control()
    rows = []
    for edge, mean, sharpe in ((0.0, -2.0, -0.2), (2.0, -1.0, 0.1), (4.0, 1.0, 0.31), (6.0, 2.0, 0.5)):
        for seed in range(5):
            rows.append({"edge_bp": edge, "seed": seed,
                         "means": [mean + seed * 0.01] * 5, "sharpes": [sharpe] * 5})
    chosen = c.select_edges(rows)
    assert (chosen["unprofitable_bp"], chosen["strong_bp"]) == (2.0, 4.0)
