"""The synthetic options surface: arbitrage-free baseline, planted violation.

**What these tests are really protecting.** The whole exercise rests on the
baseline containing no arbitrage. If it does, a proposer finds violations nobody
planted, the oracle describes none of them, and no result from the campaign
means anything. So the first test is not a sanity check -- it is the premise.

It has already earned its place. The first version of `vertical_violations`
priced both bounds off the BUY cost and reported 168 violations on a surface
that is arbitrage-free by construction. The baseline caught a bug in the
checker, which is the direction that only works if the baseline is provable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from src.core.data.options import (                              # noqa: E402
    ALL_CHECKS, OptionQuote, SyntheticOptionsSpec, SyntheticOptionsSurface,
    butterfly_violations, calendar_violations, parity_violations,
    vertical_violations,
)
from src.core.types import DataCapabilities                      # noqa: E402

FORWARDS = (100.0, 100.25, 97.5, 103.75)


def _all_violations(surface, forward):
    q = surface.quotes_at(0, 0, forward)
    return {k: (fn(q, forward) if k == "parity" else fn(q))
            for k, fn in ALL_CHECKS.items()}


# --------------------------------------------------------------------------- #
# the premise
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spacing", (0.5, 1.0, 2.5))
@pytest.mark.parametrize("vol", (0.5, 1.0, 2.0))
@pytest.mark.parametrize("forward", FORWARDS)
def test_the_baseline_contains_no_arbitrage(spacing, vol, forward):
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(
        arb_strength=0.0, strike_spacing=spacing, vol_per_session=vol))
    found = _all_violations(s, forward)
    assert not any(found.values()), (
        "the arbitrage-free surface is not arbitrage-free: "
        + "; ".join(f"{k}={v[:1]}" for k, v in found.items() if v))


def test_parity_is_exact_in_the_model_price_not_merely_close():
    """C - P = F - K is an identity here, so it holds to floating point.

    **Asserted on `price()`, not on `OptionQuote.mid`, and the difference is
    not pedantry.** A quote's bid is clamped at zero -- a real bid cannot be
    negative -- so for deep options `(bid + ask) / 2` is not the model mid, and
    an earlier version of this test failed by 0.025 at the wings for that
    reason. The clamp is correct and creates no executable violation, which
    `test_the_baseline_contains_no_arbitrage` is what actually establishes. The
    identity belongs to the pricing, so the pricing is what is checked.
    """
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(arb_strength=0.0))
    fwd = 100.0
    worst = 0.0
    for expiry in s.spec.expiries_in_sessions:
        for strike in s.strikes(fwd):
            c = s.price(fwd, strike, expiry, True)
            p_ = s.price(fwd, strike, expiry, False)
            worst = max(worst, abs((c - p_) - (fwd - strike)))
    assert worst < 1e-9, f"parity is off by {worst:g}; the density is wrong"


def test_the_zero_bid_clamp_is_where_quote_mids_stop_being_model_mids():
    """Documented rather than discovered twice. The clamp bites exactly where
    the model price is under the half-spread, and nowhere else."""
    spec = SyntheticOptionsSpec(arb_strength=0.0)
    s = SyntheticOptionsSurface(spec)
    fwd = 100.0
    clamped = unclamped = 0
    for q in s.quotes_at(0, 0, fwd):
        model = s.price(fwd, q.strike, q.expiry_session, q.is_call)
        if model < spec.half_spread:
            assert q.bid == 0.0, "a price under the half-spread must clamp"
            clamped += 1
        else:
            # 1e-6, not 1e-9: `quotes_at` rounds each side to six decimals
            # because a quote is a discrete number, so the mid can sit half a
            # rounding step from the model price. Tightening this to float
            # epsilon asserts something about the pricing using a quantity that
            # has been quantised on the way out.
            assert abs(q.mid - model) < 1e-6, "an unclamped mid must be the model mid"
            unclamped += 1
    assert clamped and unclamped, (
        f"the test is vacuous: clamped={clamped} unclamped={unclamped}")


def test_the_density_is_one_process_so_the_expiries_cannot_disagree():
    """Variance must increase with the expiry, which is what excludes calendar
    arbitrage. Drawing each expiry independently is the mistake this prevents."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec())
    offs = s._offsets
    variances = []
    for n in s.spec.expiries_in_sessions:
        d = s.density(n)
        assert abs(d.sum() - 1.0) < 1e-12, "density is not normalised"
        mean = float((d * offs).sum())
        assert abs(mean) < 1e-9, f"the forward is not a martingale: mean {mean:g}"
        variances.append(float((d * (offs - mean) ** 2).sum()))
    assert variances == sorted(variances), f"variance is not increasing: {variances}"


# --------------------------------------------------------------------------- #
# the plant
# --------------------------------------------------------------------------- #
def test_nothing_is_findable_when_the_plant_is_under_the_spread():
    """A violation that cannot be monetised must not be reported. This is the
    configuration that says whether a loop claims edges it cannot capture."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(arb_strength=0.08,
                                                    half_spread=0.05))
    found = _all_violations(s, 100.0)
    assert not any(found.values()), f"reported an uncapturable edge: {found}"
    assert s.oracle()["findable"] is False


@pytest.mark.parametrize("strength,expected", [(0.15, 0.05), (0.20, 0.10),
                                               (0.30, 0.20), (0.50, 0.40)])
def test_the_oracle_equals_what_the_checker_finds(strength, expected):
    """They are derived independently -- one from the spec, one from the quotes
    -- and they disagreed by exactly 2 * half_spread until the oracle stopped
    charging four legs for a two-leg package."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(arb_strength=strength,
                                                     half_spread=0.05))
    found = parity_violations(s.quotes_at(0, 0, 100.0), 100.0)
    assert len(found) == 1, f"expected exactly one parity violation, got {found}"
    assert found[0]["edge"] == pytest.approx(expected, abs=1e-9)
    assert s.oracle()["net_per_unit"] == pytest.approx(expected, abs=1e-9)


def test_the_plant_lands_where_the_spec_says():
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(
        arb_strength=0.3, arb_expiry_index=1, arb_strike_offset=3))
    found = parity_violations(s.quotes_at(0, 0, 100.0), 100.0)
    assert len(found) == 1
    expiry = s.spec.expiries_in_sessions[1]
    assert found[0]["expiry_session"] == expiry
    assert found[0]["strike"] == pytest.approx(103.0)


@pytest.mark.parametrize("strength,routes", [
    (0.20, ["parity"]),
    (0.30, ["parity"]),
    (0.50, ["parity", "butterfly", "calendar"]),
])
def test_the_strength_decides_how_many_routes_see_the_plant(strength, routes):
    """The docstring's difficulty table, asserted. An early draft claimed the
    plant broke parity and nothing else; shifting one option far enough makes
    its own strike ladder non-convex and its own expiry ladder non-monotone."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(arb_strength=strength,
                                                     half_spread=0.05))
    found = _all_violations(s, 100.0)
    seen = [k for k in ("parity", "butterfly", "calendar", "vertical") if found[k]]
    assert seen == routes, f"at {strength} the routes are {seen}, not {routes}"
    assert s.oracle()["visible_routes"] == routes


# --------------------------------------------------------------------------- #
# the checker's own correctness
# --------------------------------------------------------------------------- #
def test_each_check_prices_the_side_it_would_actually_hit():
    """**Regression, 2026-10-24.** `vertical_violations` tested the BUY cost
    against both bounds and reported 168 violations on the arbitrage-free
    surface. Paying more than a spread's width to own it is a bad trade, not an
    arbitrage; the arbitrage is SELLING it above its maximum liability, and the
    proceeds are 2 * half_spread below the buy cost. Losing that distinction is
    silent -- every number still looks plausible."""
    import inspect
    src = inspect.getsource(vertical_violations)
    assert "proceeds_sell" in src and "cost_buy" in src, (
        "the vertical check no longer separates the two directions")
    flat = " ".join(src.split())
    assert "proceeds_sell - width > tol" in flat, (
        "the upper bound must be tested against sale proceeds, not the buy cost")


def test_a_hand_built_violation_is_caught_by_each_check():
    """Each checker is fed a quote set that violates only its own condition, so
    a check that silently stopped firing is caught."""
    def q(expiry, strike, is_call, bid, ask):
        return OptionQuote(ts=0, session=0, expiry_session=expiry, strike=strike,
                           is_call=is_call, bid=bid, ask=ask)
    # a call spread you are PAID to own
    assert vertical_violations([q(5, 100.0, True, 1.0, 1.1),
                                q(5, 101.0, True, 1.4, 1.5)])
    # a butterfly bought below zero
    assert butterfly_violations([q(5, 99.0, True, 1.0, 1.0),
                                 q(5, 100.0, True, 2.0, 2.0),
                                 q(5, 101.0, True, 1.0, 1.0)])
    # the near expiry bid above the far expiry ask
    assert calendar_violations([q(5, 100.0, True, 3.0, 3.1),
                                q(10, 100.0, True, 1.0, 1.1)])
    # parity broken at one pair
    assert parity_violations([q(5, 100.0, True, 0.0, 0.0),
                              q(5, 100.0, False, 0.0, 0.0)], 101.0)


# --------------------------------------------------------------------------- #
# the contract
# --------------------------------------------------------------------------- #
def test_the_surface_is_deterministic_for_a_spec():
    a = SyntheticOptionsSurface(SyntheticOptionsSpec(arb_strength=0.3))
    b = SyntheticOptionsSurface(SyntheticOptionsSpec(arb_strength=0.3))
    assert a.spec.digest() == b.spec.digest()
    assert a.quotes_at(7, 1, 101.25) == b.quotes_at(7, 1, 101.25)


def test_a_different_plant_is_a_different_dataset():
    a = SyntheticOptionsSpec(arb_strength=0.0).digest()
    b = SyntheticOptionsSpec(arb_strength=0.3).digest()
    assert a != b, "the plant must change the digest or two datasets are one"


def test_options_are_a_declared_capability():
    """A fill model or strategy needing the surface is refused at wiring time,
    like the other three, rather than discovering it mid-run."""
    assert DataCapabilities().has_options is False
    trades_only = DataCapabilities(has_trades=True)
    needs_options = DataCapabilities(has_trades=True, has_options=True)
    assert not trades_only.satisfies(needs_options)
    assert needs_options.satisfies(trades_only)
    assert "has_options" in DataCapabilities().as_dict()


@pytest.mark.parametrize("kw", [{"n_strikes": 40},
                                {"expiries_in_sessions": (5, 2)},
                                {"expiries_in_sessions": (0, 1)},
                                {"arb_expiry_index": 99}])
def test_an_incoherent_surface_is_refused_at_construction(kw):
    with pytest.raises(ValueError):
        SyntheticOptionsSurface(SyntheticOptionsSpec(**kw))
