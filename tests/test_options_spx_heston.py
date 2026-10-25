"""The SPX surface from Michael's Heston calibration (2026-10-12 13:56:47 CT).

Two things are being protected. The first is the same premise as the Gaussian
surface: if the baseline contains arbitrage nobody planted, the oracle describes
none of it and no campaign result means anything. The second is new -- that the
surface actually REPRODUCES the calibration, because a Heston implementation
that runs without error and prices the wrong thing is the expensive failure
here, and the Feller condition is violated by these parameters so the
characteristic function has a branch that can go silently wrong.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from src.core.data.options import (                              # noqa: E402
    Plant, butterfly_violations, calendar_violations, vertical_violations,
)
from src.core.data.options_spx import (                          # noqa: E402
    ETA, KAPPA, MATURITIES, RHO, SPOT, SPXHestonSurface, SPXSpec, THETA, V0,
    sessions_to_expiry,
)


@pytest.fixture(scope="module")
def surface():
    return SPXHestonSurface()


# --------------------------------------------------------------------------- #
# the calibration arrived consistent, and the model reproduces it
# --------------------------------------------------------------------------- #
def test_the_calibration_is_internally_consistent():
    """Each forward reconstructs from spot, r, q and the T implied by its own
    discount factor. Checked on arrival rather than assumed: a quote sheet whose
    forwards and discounts disagree would put that disagreement into every
    price, invisibly."""
    for m in MATURITIES:
        T = -math.log(m.discount)/m.r
        assert abs(T - m.T) < 1e-12
        rebuilt = SPOT*math.exp((m.r - m.q)*T)
        assert abs(rebuilt - m.forward) < 1.5, (
            f"{m.label}: forward {m.forward} vs {rebuilt:.2f} from S, r, q")


def test_feller_is_violated_so_the_little_trap_form_is_required():
    """2*kappa*theta = 0.2268 against eta^2 = 0.9624. The naive branch,
    g = (xi + d)/(xi - d), has modulus above one and its logarithm wraps at long
    maturities -- producing wrong prices that look entirely reasonable."""
    assert 2*KAPPA*THETA < ETA**2
    import inspect
    src = inspect.getsource(SPXHestonSurface._cf)
    assert "(xi - d)/(xi + d)" in src, "the characteristic function lost the safe branch"


@pytest.mark.parametrize("i", range(len(MATURITIES)))
def test_the_characteristic_function_is_a_martingale(surface, i):
    """phi(-i) = 1 IS the martingale condition, and it is the check that the
    branch is right: a wrapped logarithm breaks it immediately."""
    assert abs(complex(SPXHestonSurface._cf(-1j, MATURITIES[i].T)) - 1.0) < 1e-10
    assert abs(complex(SPXHestonSurface._cf(0.0, MATURITIES[i].T)) - 1.0) < 1e-12


@pytest.mark.parametrize("i", range(len(MATURITIES)))
def test_the_density_is_a_valid_probability_measure(surface, i):
    """Every no-arbitrage guarantee rests on this and nothing else."""
    terminal, prob = surface.density(i)
    assert prob.min() >= 0.0, f"negative density at {MATURITIES[i].label}"
    assert abs(prob.sum() - 1.0) < 1e-9
    assert abs(float(terminal @ prob) - MATURITIES[i].forward) < 0.1, (
        "E[S_T] is not the forward; the measure is not risk-neutral")


def test_the_surface_reproduces_the_fitted_shape(surface):
    """v0 = 0.0211 (14.5% vol) reverting to theta = 0.0542 (23.3%) with a
    four-month half-life, and rho = -0.667. So: a rising ATM term structure and
    a steep front skew that flattens. Measured, with generous bands -- this is
    a shape check, not a re-fit."""
    atm, skew = [], []
    for i, m in enumerate(MATURITIES):
        ks = surface.strikes(i)
        pick = lambda f: min(ks, key=lambda k: abs(k - f*m.forward))   # noqa: E731
        atm.append(surface.implied_vol(i, pick(1.00)))
        skew.append(surface.implied_vol(i, pick(0.90)) - surface.implied_vol(i, pick(1.10)))
    assert atm == sorted(atm), f"ATM term structure is not rising: {atm}"
    assert 0.12 < atm[0] < 0.16, f"front ATM vol {atm[0]:.4f} is not near sqrt(v0)"
    assert 0.17 < atm[-1] < 0.22, f"long ATM vol {atm[-1]:.4f} is not heading to sqrt(theta)"
    assert all(s > 0 for s in skew), f"rho < 0 must give downside vol above upside: {skew}"
    assert skew[0] > 0.06, f"front skew {skew[0]:.4f} is too flat for rho = {RHO}"
    assert skew == sorted(skew, reverse=True), f"skew must flatten with maturity: {skew}"


# --------------------------------------------------------------------------- #
# the premise
# --------------------------------------------------------------------------- #
def test_the_baseline_contains_no_arbitrage(surface):
    v = surface.violations()["routes_seen"]
    assert v == {"parity": 0, "vertical": 0, "butterfly": 0}, v


def test_parity_is_exact_with_discounting(surface):
    """C - P = D (F - K). The Gaussian surface took r = 0 and could leave the
    discount factor out; here D runs to 0.79944 and omitting it would break
    parity at every strike in proportion to moneyness."""
    worst = 0.0
    for i, m in enumerate(MATURITIES):
        ks = surface.strikes(i)
        c = surface.call_prices(i, ks)
        p = surface.put_prices(i, ks)
        for j, k in enumerate(ks):
            worst = max(worst, abs((c[j] - p[j]) - m.discount*(m.forward - k)))
    assert worst < 1e-9, f"parity is off by {worst:g}"


def test_quotes_round_away_from_the_mid(surface):
    for q in surface.quotes_at(0, 0):
        # `expiry_session` is the expiry as a session count, not a position in
        # the maturity list -- `maturity_index` is the way back, and exercising
        # it here is the point rather than an inconvenience.
        i = surface.maturity_index(q.expiry_session)
        mid = float((surface.call_prices(i, [q.strike]) if q.is_call
                     else surface.put_prices(i, [q.strike]))[0])
        h = surface.half_spread(mid, i)
        inc = surface.increment(mid)
        assert q.bid <= max(0.0, mid - h) + 1e-9, "the bid rounded towards the mid"
        assert q.ask >= mid + h - 1e-9, "the ask rounded towards the mid"
        assert q.ask - (mid + h) < inc + 1e-9


# --------------------------------------------------------------------------- #
# the calendar finding
# --------------------------------------------------------------------------- #
def test_the_same_strike_calendar_check_is_invalid_here_and_is_not_run(surface):
    """**The eighth bug the arbitrage-free baseline has caught, and the first
    where the check was right somewhere else.**

    `calendar_violations` is correct on the Gaussian surface, where r = q = 0
    and every expiry shares a forward. Here r > q, so the forward rises with
    maturity and the same strike is legitimately deeper in the money at the near
    date: the Dec-26 put at 8575 is worth more than the Dec-27 one. Selling the
    near and buying the far replicates nothing -- after the near expires you
    have paid out, and the far option's value then is a random variable.

    This test asserts the check WOULD fire, so that the exclusion stays a
    documented judgement rather than drifting into an untested assumption.
    """
    naive = calendar_violations(surface.quotes_at(0, 0))
    assert len(naive) > 50, (
        "the naive same-strike check no longer fires; if the surface changed so "
        "that it is valid, re-enable it deliberately rather than leaving this "
        "test asserting nothing")
    assert all(not r["is_call"] for r in naive), (
        "the false positives were all puts, which is what rising forwards "
        "predicts; calls firing too would mean something else is wrong")
    assert "calendar" not in surface.violations()["routes_seen"]


def test_a_calendar_plant_is_refused_with_its_reason():
    with pytest.raises(ValueError, match="carry"):
        SPXHestonSurface(SPXSpec(plants=(Plant("calendar", 0, 0, 50.0),)))


# --------------------------------------------------------------------------- #
# the plants
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("strength", (5.0, 20.0, 60.0))
def test_a_parity_plant_above_its_threshold_is_found_inside_its_bound(strength):
    s = SPXHestonSurface(SPXSpec(plants=(Plant("parity", 0, 0, strength),)))
    row = s.oracle()["plants"][0]
    assert row["findable"], row
    assert row["intrinsic_to_clear"] == pytest.approx(0.0, abs=1e-9), (
        "only parity has a zero intrinsic term, because only parity is an identity")
    front = sessions_to_expiry(MATURITIES[0])
    found = s._parity_for(
        [q for q in s.quotes_at(0, 0) if q.expiry_session == front],
        MATURITIES[0])
    assert len(found) == 1, found
    assert found[0]["edge"] <= row["net_per_unit_upper"] + 1e-9
    assert row["net_per_unit_upper"] - found[0]["edge"] <= 2*0.10 + 1e-9, (
        "the gap to the bound is larger than quantisation explains")


def test_a_plant_under_its_threshold_is_not_reported():
    s = SPXHestonSurface(SPXSpec(plants=(Plant("parity", 0, 0, 0.5),)))
    assert s.oracle()["plants"][0]["findable"] is False
    assert s.violations()["routes_seen"] == {"parity": 0, "vertical": 0, "butterfly": 0}


# --------------------------------------------------------------------------- #
# the contract
# --------------------------------------------------------------------------- #
def test_the_ladder_is_shaped_like_spx(surface):
    ks = surface.strikes(0)
    f = MATURITIES[0].forward
    near = {b - a for a, b in zip(ks, ks[1:]) if abs(a - f) < 0.04*f}
    wide = {b - a for a, b in zip(ks, ks[1:]) if abs(a - f) > 0.10*f}
    assert near == {5.0}, f"near-money spacing is {near}"
    assert wide == {25.0}, f"wide spacing is {wide}"


def test_the_plant_changes_the_digest():
    a = SPXSpec().digest()
    b = SPXSpec(plants=(Plant("parity", 0, 0, 20.0),)).digest()
    assert a != b


def test_the_surface_is_deterministic():
    a, b = SPXHestonSurface(), SPXHestonSurface()
    assert a.quotes_at(5, 1) == b.quotes_at(5, 1)
