"""The synthetic QQQ options surface: arbitrage-free baseline, planted violations.

**What these tests are really protecting.** The whole exercise rests on the
baseline containing no arbitrage. If it does, a proposer finds violations nobody
planted, the oracle describes none of them, and no result from the campaign
means anything. So the first test is not a sanity check -- it is the premise.

It has already caught three bugs, all mine, and all in the direction that only
works when the baseline is a theorem rather than a hope:

* `vertical_violations` priced both bounds off the BUY cost and reported 168
  violations. Paying over a spread's width to own it is a bad trade; the
  arbitrage is selling it above its maximum liability.
* `butterfly_violations` assumed equal spacing and reported 6 violations at the
  $1/$5 strike boundary, where consecutive triples are not butterflies.
* the oracle charged spreads only, so a calendar plant of 0.45 was reported
  findable when the calendar premium alone was 1.10.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from src.core.data.options import (                              # noqa: E402
    ALL_CHECKS, OptionQuote, Plant, SyntheticOptionsSpec, SyntheticOptionsSurface,
    butterfly_violations, calendar_violations, parity_violations,
    vertical_violations,
)
from src.core.types import DataCapabilities                      # noqa: E402

FORWARDS = (500.0, 500.37, 487.5, 512.25, 533.0)


def _violations(surface, forward):
    q = surface.quotes_at(0, 0, forward)
    return {k: (fn(q, forward) if k == "parity" else fn(q))
            for k, fn in ALL_CHECKS.items()}


# --------------------------------------------------------------------------- #
# the premise
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("vol", (3.0, 5.0, 9.0))
@pytest.mark.parametrize("near", (10.0, 20.0))
@pytest.mark.parametrize("forward", FORWARDS)
def test_the_baseline_contains_no_arbitrage(vol, near, forward):
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(
        vol_per_session=vol, near_halfwidth=near))
    found = _violations(s, forward)
    assert not any(found.values()), (
        "the arbitrage-free surface is not arbitrage-free: "
        + "; ".join(f"{k}={v[:1]}" for k, v in found.items() if v))


def test_parity_is_exact_in_the_model_price():
    """C - P = F - K is an identity, so it holds to floating point.

    Asserted on `price()` and not on `OptionQuote.mid`: a bid is clamped at
    zero and both sides are quantised, so a quote mid is not the model mid.
    The identity belongs to the pricing, so the pricing is what is checked.
    """
    s = SyntheticOptionsSurface(SyntheticOptionsSpec())
    fwd = 500.0
    worst = 0.0
    for expiry in s.spec.expiries_in_sessions:
        for strike in s.strikes(fwd):
            c = s.price(fwd, strike, expiry, True)
            p_ = s.price(fwd, strike, expiry, False)
            worst = max(worst, abs((c - p_) - (fwd - strike)))
    assert worst < 1e-9, f"parity is off by {worst:g}; the density is wrong"


def test_the_expiries_cannot_disagree_because_they_are_one_process():
    """Gaussians are infinitely divisible, so a single draw per expiry at
    sd = s*sqrt(T) IS the shorter expiry convolved further. The forward must be
    a martingale and the variance must increase, which is what excludes
    calendar arbitrage."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec())
    variances = []
    for n in s.spec.expiries_in_sessions:
        offs, prob = s.density(n)
        assert abs(prob.sum() - 1.0) < 1e-12, "density is not normalised"
        mean = float((prob * offs).sum())
        assert abs(mean) < 1e-9, f"the forward is not a martingale: mean {mean:g}"
        variances.append(float((prob * (offs - mean) ** 2).sum()))
    assert variances == sorted(variances), f"variance is not increasing: {variances}"


def test_quantisation_rounds_away_from_the_mid_and_can_only_cost_edge():
    """**A correctness property, not a convention.** Rounding to nearest would
    move a bid up or an ask down by half an increment, manufacturing up to 0.05
    per leg on the nickel grid -- larger than some plants. Bids floor, asks
    ceil, so quantisation can only make an arbitrage harder."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec())
    fwd = 500.0
    checked = 0
    for q in s.quotes_at(0, 0, fwd):
        mid = s.price(fwd, q.strike, q.expiry_session, q.is_call)
        h = s.half_spread(mid, q.expiry_session)
        inc = s.increment(mid)
        assert q.bid <= max(0.0, mid - h) + 1e-9, "the bid rounded towards the mid"
        assert q.ask >= mid + h - 1e-9, "the ask rounded towards the mid"
        assert q.ask - (mid + h) < inc + 1e-9, "the ask moved more than one increment"
        checked += 1
    assert checked > 500, f"only {checked} quotes checked; the surface shrank"


def test_the_quote_grid_is_the_opra_one():
    """A penny under $3.00 of premium, a nickel at or above it."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec())
    assert s.increment(0.50) == pytest.approx(0.01)
    assert s.increment(2.99) == pytest.approx(0.01)
    assert s.increment(3.00) == pytest.approx(0.05)
    assert s.increment(42.0) == pytest.approx(0.05)
    for q in s.quotes_at(0, 0, 500.0):
        inc = 0.05 if q.ask >= 3.0 else 0.01
        assert abs(round(q.ask / inc) * inc - q.ask) < 1e-6, (
            f"ask {q.ask} is not on the {inc} grid")


def test_the_strike_ladder_is_shaped_like_a_real_one():
    """$1 near the money, $5 in the wings. A uniform grid hides the thing that
    broke the butterfly check: the spacing change."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec())
    ks = s.strikes(500.0)
    assert 500.0 in ks
    near = [b - a for a, b in zip(ks, ks[1:]) if abs(a - 500.0) < 15]
    wings = [b - a for a, b in zip(ks, ks[1:]) if abs(a - 500.0) > 40]
    assert near and set(near) == {1.0}, f"near-money spacing is {set(near)}"
    assert wings and set(wings) == {5.0}, f"wing spacing is {set(wings)}"
    assert ks[0] <= 430.0 and ks[-1] >= 570.0, "the wings do not reach far enough"


# --------------------------------------------------------------------------- #
# the plants
# --------------------------------------------------------------------------- #
PLANTS = (Plant("parity", 2, 0, 0.60),
          Plant("butterfly", 4, -5, 0.90),
          Plant("calendar", 1, 8, 1.60))


@pytest.mark.parametrize("plant", PLANTS, ids=lambda p: p.kind)
def test_a_plant_above_its_threshold_fires_at_its_own_site(plant):
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(plants=(plant,)))
    fwd = 500.0
    row = s.oracle(fwd)["plants"][0]
    assert row["findable"], f"the test plant is under its own threshold: {row}"
    fn = ALL_CHECKS[plant.kind]
    found = fn(s.quotes_at(0, 0, fwd), fwd) if plant.kind == "parity" \
        else fn(s.quotes_at(0, 0, fwd))
    at_site = [r for r in found
               if r.get("strike") == row["strike"]
               or (isinstance(r.get("strikes"), tuple) and row["strike"] in r["strikes"])]
    assert at_site, f"{plant.kind} planted at {row['strike']} was not found: {found}"
    assert max(r["edge"] for r in at_site) <= row["net_per_unit_upper"] + 1e-9, (
        "the found edge exceeds the oracle's upper bound")


@pytest.mark.parametrize("plant", PLANTS, ids=lambda p: p.kind)
def test_the_found_edge_is_within_quantisation_of_the_bound(plant):
    """The bound is not loose. Quantisation is the only thing between them, so
    the gap must be under one increment per leg."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(plants=(plant,)))
    fwd = 500.0
    row = s.oracle(fwd)["plants"][0]
    fn = ALL_CHECKS[plant.kind]
    found = fn(s.quotes_at(0, 0, fwd), fwd) if plant.kind == "parity" \
        else fn(s.quotes_at(0, 0, fwd))
    best = max((r["edge"] for r in found), default=0.0)
    slack = row["net_per_unit_upper"] - best
    assert 0.0 <= slack <= row["legs"] * 0.05 + 1e-9, (
        f"{plant.kind}: bound {row['net_per_unit_upper']:.4f} vs found {best:.4f}; "
        f"slack {slack:.4f} is not explained by quantisation")


def test_a_plant_under_its_threshold_is_not_reported():
    """A violation that cannot be monetised must not be found. This is the
    configuration that says whether a loop claims edges it cannot capture."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(
        plants=(Plant("parity", 2, 0, 0.10),)))
    row = s.oracle(500.0)["plants"][0]
    assert row["findable"] is False, f"0.10 should be under the threshold: {row}"
    assert not any(_violations(s, 500.0).values())


def test_only_parity_has_no_intrinsic_term_to_clear():
    """Parity is an identity, so `C - P - (F - K)` is exactly zero before the
    plant and the spread is all there is. A calendar plant must first clear the
    premium between the two expiries -- 1.10 at the 2-vs-5 session pair -- and a
    butterfly plant the convexity already in the ladder. Assuming spreads were
    the whole cost reported a 0.45 calendar plant as findable when it produced
    no calendar violation at all."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(plants=PLANTS))
    by = {r["kind"]: r for r in s.oracle(500.0)["plants"]}
    assert by["parity"]["intrinsic_to_clear"] == pytest.approx(0.0, abs=1e-9)
    assert by["calendar"]["intrinsic_to_clear"] > 0.5, (
        "the calendar premium is being ignored again")
    assert by["butterfly"]["intrinsic_to_clear"] > 0.0


def test_the_oracle_reports_routes_it_measured_not_routes_it_predicted():
    """A plant leaks: shifting one option far enough makes its strike ladder
    non-convex and its expiry ladder non-monotone, so one plant can appear in
    parity, vertical and butterfly at once. Predicting that analytically would
    be a second model to be wrong in."""
    s = SyntheticOptionsSurface(SyntheticOptionsSpec(plants=PLANTS))
    o = s.oracle(500.0)
    assert set(o["routes_seen"]) == set(ALL_CHECKS)
    assert sum(o["routes_seen"].values()) > len(PLANTS), (
        "three plants should leak into more than three violations")
    assert o["routes_seen"]["calendar"] >= 1


def test_plants_and_the_shorthand_are_not_both_allowed():
    with pytest.raises(ValueError, match="not both"):
        SyntheticOptionsSpec(plants=(Plant("parity", 1, 0, 0.5),),
                             arb_strength=0.3).all_plants()
    assert SyntheticOptionsSpec(arb_strength=0.3).all_plants()[0].kind == "parity"


# --------------------------------------------------------------------------- #
# the checkers' own correctness
# --------------------------------------------------------------------------- #
def test_the_vertical_check_prices_the_side_it_would_actually_hit():
    """**Regression.** It tested the BUY cost against both bounds and reported
    168 violations on the arbitrage-free surface."""
    import inspect
    flat = " ".join(inspect.getsource(vertical_violations).split())
    assert "proceeds_sell" in flat and "cost_buy" in flat
    assert "proceeds_sell - width > tol" in flat


def test_the_butterfly_check_handles_unequal_spacing():
    """**Regression.** The 1-2-1 form reported 6 violations at the $1/$5
    boundary, where (475, 480, 481) is not a butterfly. Convexity still holds
    there with weights w = (K3-K2)/(K3-K1)."""
    import inspect
    flat = " ".join(inspect.getsource(butterfly_violations).split())
    assert "weight_low" in flat and "c.strike - b.strike" in flat
    def q(k, bid, ask):
        return OptionQuote(ts=0, session=0, expiry_session=5, strike=k,
                           is_call=True, bid=bid, ask=ask)
    # equally spaced, genuinely non-convex
    assert butterfly_violations([q(99.0, 1.0, 1.0), q(100.0, 2.0, 2.0),
                                 q(101.0, 1.0, 1.0)])
    # unequally spaced and convex: must NOT fire
    assert not butterfly_violations([q(475.0, 26.0, 26.1), q(480.0, 21.2, 21.3),
                                     q(481.0, 20.3, 20.4)])


def test_a_hand_built_violation_is_caught_by_each_check():
    def q(expiry, strike, is_call, bid, ask):
        return OptionQuote(ts=0, session=0, expiry_session=expiry, strike=strike,
                           is_call=is_call, bid=bid, ask=ask)
    assert vertical_violations([q(5, 100.0, True, 1.0, 1.1),
                                q(5, 101.0, True, 1.4, 1.5)])
    assert calendar_violations([q(5, 100.0, True, 3.0, 3.1),
                                q(10, 100.0, True, 1.0, 1.1)])
    assert parity_violations([q(5, 100.0, True, 0.0, 0.0),
                              q(5, 100.0, False, 0.0, 0.0)], 101.0)


# --------------------------------------------------------------------------- #
# the contract
# --------------------------------------------------------------------------- #
def test_the_surface_is_deterministic_for_a_spec():
    a = SyntheticOptionsSurface(SyntheticOptionsSpec(plants=PLANTS))
    b = SyntheticOptionsSurface(SyntheticOptionsSpec(plants=PLANTS))
    assert a.spec.digest() == b.spec.digest()
    assert a.quotes_at(7, 1, 501.25) == b.quotes_at(7, 1, 501.25)


def test_a_different_plant_is_a_different_dataset():
    a = SyntheticOptionsSpec().digest()
    b = SyntheticOptionsSpec(plants=PLANTS).digest()
    c = SyntheticOptionsSpec(plants=PLANTS[:1]).digest()
    assert len({a, b, c}) == 3, "the plants must change the digest"


def test_options_are_a_declared_capability():
    assert DataCapabilities().has_options is False
    trades_only = DataCapabilities(has_trades=True)
    needs_options = DataCapabilities(has_trades=True, has_options=True)
    assert not trades_only.satisfies(needs_options)
    assert needs_options.satisfies(trades_only)
    assert "has_options" in DataCapabilities().as_dict()


@pytest.mark.parametrize("kw", [
    {"expiries_in_sessions": (5, 2)},
    {"expiries_in_sessions": (0, 1)},
    {"wing_halfwidth": 5.0},
    {"plants": (Plant("nonsense", 1, 0, 0.5),)},
    {"plants": (Plant("parity", 99, 0, 0.5),)},
    {"plants": (Plant("calendar", 6, 0, 0.5),)},     # no further expiry
])
def test_an_incoherent_surface_is_refused_at_construction(kw):
    with pytest.raises(ValueError):
        SyntheticOptionsSurface(SyntheticOptionsSpec(**kw))
