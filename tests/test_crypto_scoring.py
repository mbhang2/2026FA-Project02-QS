"""Amendment 16: crypto scoring on notional, with funding -- and NQ left untouched.

The properties that make it right or quietly wrong: a return is the same number
whatever the price level; funding is charged on the position actually held at
the settlement, with the right sign; the null's no-flip draw still equals the
observed score under the new terms; and the `points` path is bit-identical to
what it was.
"""

from __future__ import annotations

import pytest

from src.core.scoring import metrics
from src.core.scoring.metrics import FundingEvent, SessionSpan
from src.core.scoring.randomization import round_turn as rt
from src.core.scoring.scorer import score
from src.core.scoring.terms import scoring_terms
from src.core.types import Blotter, BlotterRow, Side

DAY = 86_400 * 10**9


def _cal(n: int) -> list[SessionSpan]:
    return [SessionSpan(i, i * DAY, (i + 1) * DAY - 1) for i in range(n)]


def _blotter(trades, *, commission=0.0) -> Blotter:
    """trades: (session, entry, exit, size) round turns, long, held 01:00 -> 23:00."""
    b, seq = Blotter(), 0
    for s, entry, exit_, size in trades:
        for ts, price, side, pos, pnl in (
                (s * DAY + 3_600 * 10**9, entry, Side.BUY, size, 0.0),
                (s * DAY + 23 * 3_600 * 10**9, exit_, Side.SELL, 0, (exit_ - entry) * size)):
            seq += 1
            b.add(BlotterRow(seq=seq, ts=ts, order_id=seq, side=side, price=price, size=size,
                             position_after=pos, commission=commission, realized_pnl=pnl))
    return b


def test_a_return_on_notional_does_not_depend_on_the_price_level():
    cal = _cal(40)
    low = _blotter([(i, 16_000.0, 16_000.0 * (1.01 if i % 3 else 0.99), 1) for i in range(40)])
    high = _blotter([(i, 100_000.0, 100_000.0 * (1.01 if i % 3 else 0.99), 1) for i in range(40)])
    r_low = metrics.session_returns(low, cal, notional_base={i: 16_000.0 for i in range(40)},
                                    idle_charge=0.0)
    r_high = metrics.session_returns(high, cal, notional_base={i: 100_000.0 for i in range(40)},
                                     idle_charge=0.0)
    assert r_low == pytest.approx(r_high)
    assert r_low[1] == pytest.approx(0.01)


def test_funding_is_paid_on_the_position_held_at_the_settlement():
    cal = _cal(2)
    b = _blotter([(0, 100.0, 100.0, 3)])        # long 3 lots from 01:00 to 23:00 of session 0
    events = [FundingEvent(ts=0, rate=0.001, mark=100.0),             # 00:00, flat
              FundingEvent(ts=8 * 3_600 * 10**9, rate=0.001, mark=100.0),   # held
              FundingEvent(ts=16 * 3_600 * 10**9, rate=-0.002, mark=110.0),  # held, negative
              FundingEvent(ts=DAY, rate=0.001, mark=100.0)]                # next day, flat
    paid = metrics.funding_points(b, cal, events)
    assert paid[0] == pytest.approx(3 * 100.0 * 0.001 + 3 * 110.0 * -0.002)
    assert paid[1] == 0.0
    r = metrics.session_returns(b, cal, notional_base={0: 100.0, 1: 100.0}, idle_charge=0.0,
                                funding=events)
    assert r[0] == pytest.approx(-paid[0] / 100.0)


def test_a_short_receives_a_positive_rate():
    cal = _cal(1)
    b = Blotter()
    b.add(BlotterRow(seq=1, ts=3_600 * 10**9, order_id=1, side=Side.SELL, price=100.0, size=2,
                     position_after=-2, commission=0.0, realized_pnl=0.0))
    paid = metrics.funding_points(b, cal, [FundingEvent(ts=8 * 3_600 * 10**9, rate=0.001, mark=100.0)])
    assert paid[0] == pytest.approx(-0.2)          # negative cost: received


def test_the_null_identity_still_equals_the_observed_under_notional_terms():
    cal = _cal(40)
    b = _blotter([(i, 100.0 + i, 100.0 + i + (1 if i % 2 else -0.5), 2) for i in range(40)],
                 commission=0.05)
    terms = {"notional_base": {i: 100.0 + i for i in range(40)},
             "idle_charge": {i: 1.2e-5 * (100.0 + i) for i in range(40)},
             "funding": [FundingEvent(ts=i * DAY + 8 * 3_600 * 10**9, rate=1e-4, mark=100.0 + i)
                         for i in range(40)]}
    observed = score(b, period=cal, **terms)
    ident = rt.identity_from_blotter(b, cal, **terms)
    assert ident.primary == pytest.approx(observed.primary)
    assert len(rt.null_sample(b, cal, b=50, seed=1, **terms)) == 50


def test_points_terms_reproduce_the_previous_defaults_exactly():
    cal = _cal(40)
    b = _blotter([(i, 18_000.0, 18_000.0 + (5 if i % 2 else -3), 1) for i in range(0, 40, 2)],
                 commission=0.1)
    terms = scoring_terms({"campaign": {"idle_charge": 0.18}}, {"dataset": "nq"}, None, cal)
    assert terms == {"notional_base": 1.0, "idle_charge": 0.18, "funding": None}
    assert metrics.session_returns(b, cal, **terms) == metrics.session_returns(b, cal)
    assert rt.null_sample(b, cal, b=20, seed=3, **terms) == rt.null_sample(b, cal, b=20, seed=3)


class _FakeCrypto:
    def session_open_prices(self, sessions):
        return {s: 1_000.0 * (s + 1) for s in sessions}

    def funding_series(self, sessions):
        return [FundingEvent(ts=s * DAY, rate=1e-4, mark=1.0) for s in sessions]


def test_notional_terms_come_from_the_dataset_and_refuse_a_constant():
    cal = _cal(3)
    prof = {"scoring_units": "notional", "idle_charge_fraction": 1.2e-5, "funding": True}
    t = scoring_terms({}, prof, _FakeCrypto(), cal)
    assert t["notional_base"] == {0: 1_000.0, 1: 2_000.0, 2: 3_000.0}
    assert t["idle_charge"][2] == pytest.approx(1.2e-5 * 3_000.0)
    assert len(t["funding"]) == 3
    with pytest.raises(ValueError, match="constant notional_base"):
        scoring_terms({}, {**prof, "notional_base": 1.0}, _FakeCrypto(), cal)


def test_a_missing_session_notional_is_an_error_not_a_default():
    cal = _cal(2)
    b = _blotter([(0, 1.0, 2.0, 1)])
    with pytest.raises(KeyError, match="no per-session value"):
        metrics.session_returns(b, cal, notional_base={0: 1.0}, idle_charge=0.0)
