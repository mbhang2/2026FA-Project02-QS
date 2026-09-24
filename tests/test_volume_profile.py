"""Volume at price: VPOC, Value Area, Low Volume Nodes.

The properties worth asserting are not "the numbers are right" -- they are that
the feature is **causal**, **deterministic**, and **absent rather than zero** when
there is nothing to compute. Those are the three ways a feature like this
silently corrupts a result.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.features.volume_profile import SessionProfile  # noqa: E402


def build(pairs, tick_size=0.25):
    p = SessionProfile(tick_size)
    for price, size in pairs:
        p.add(price, size)
    return p


# --------------------------------------------------------------------------- #
# absence is None, never 0.0
# --------------------------------------------------------------------------- #
def test_an_empty_profile_has_no_vpoc_rather_than_a_vpoc_of_zero():
    """Section 2f's hazard in its feature form. A price of 0.0 for "nothing has
    traded" is a fabricated feature, and a model trained on it learns from a
    number nobody measured."""
    p = SessionProfile(0.25)
    assert p.is_empty
    assert p.vpoc() is None
    assert p.value_area() is None
    assert p.low_volume_nodes() == ()
    assert p.distance_from_vpoc(100.0) is None
    assert p.value_area_position(100.0) is None


def test_a_reset_profile_is_empty_again():
    p = build([(100.0, 5)])
    assert p.vpoc() == 100.0
    p.reset()
    assert p.is_empty and p.vpoc() is None and p.total_volume == 0


# --------------------------------------------------------------------------- #
# causality: the profile answers from what it has been given, and nothing more
# --------------------------------------------------------------------------- #
def test_the_profile_only_ever_reflects_what_has_been_added():
    """The property that makes this safe to use mid-session. A finished session's
    VPOC is not knowable while the session runs; a feature built from one is
    future data wearing a feature's clothes."""
    p = SessionProfile(0.25)
    p.add(100.0, 10)
    assert p.vpoc() == 100.0
    p.add(200.0, 50)                       # a later trade moves it
    assert p.vpoc() == 200.0
    # and the earlier answer was correct for the data that existed then
    q = build([(100.0, 10)])
    assert q.vpoc() == 100.0


def test_volume_is_conserved():
    p = build([(100.0, 5), (100.25, 7), (100.0, 3)])
    assert p.total_volume == 15


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #
def test_order_does_not_change_the_answer():
    """Integer bin arithmetic over int volumes: no float accumulation, so no
    ordering sensitivity. A profile that depended on arrival order would make two
    runs over the same ticks disagree."""
    pairs = [(100.0, 5), (100.25, 20), (100.5, 3), (100.75, 8), (101.0, 1)]
    a = build(pairs)
    b = build(list(reversed(pairs)))
    assert a.vpoc() == b.vpoc()
    assert a.value_area() == b.value_area()
    assert a.low_volume_nodes() == b.low_volume_nodes()
    assert a.total_volume == b.total_volume


def test_a_tie_for_the_vpoc_resolves_to_the_lower_price():
    """Arbitrary but stated. An unstated tie-break is a silent source of
    run-to-run difference."""
    p = build([(100.0, 10), (100.25, 10)])
    assert p.vpoc() == 100.0


# --------------------------------------------------------------------------- #
# the constructions themselves
# --------------------------------------------------------------------------- #
def test_the_vpoc_is_the_price_that_traded_most():
    p = build([(100.0, 5), (100.25, 20), (100.5, 3)])
    assert p.vpoc() == 100.25


def test_the_value_area_covers_the_requested_fraction():
    pairs = [(100.0, 5), (100.25, 20), (100.5, 3), (100.75, 8), (101.0, 1)]
    p = build(pairs)
    low, high = p.value_area(0.70)
    covered = sum(s for price, s in pairs if low <= price <= high)
    assert covered >= 0.70 * p.total_volume
    assert low <= p.vpoc() <= high, "the value area must contain the VPOC"


def test_the_whole_fraction_takes_the_whole_range():
    pairs = [(100.0, 5), (100.25, 20), (100.5, 3)]
    p = build(pairs)
    assert p.value_area(1.0) == (100.0, 100.5)


def test_a_single_price_gives_a_band_at_that_price():
    p = build([(100.0, 9)])
    assert p.value_area() == (100.0, 100.0)
    assert p.value_area_position(100.0) == 0


def test_low_volume_nodes_are_local_minima_not_merely_quiet_bins():
    """The tails of any profile are quiet without being nodes. Requiring a local
    minimum is what separates 'traded through' from 'edge of the range'."""
    # a clean valley at 100.5 between two peaks
    p = build([(100.0, 20), (100.25, 15), (100.5, 1), (100.75, 15), (101.0, 20)])
    assert p.low_volume_nodes() == (100.5,)


def test_the_edges_of_the_range_are_never_nodes():
    p = build([(100.0, 1), (100.25, 50), (100.5, 1)])
    assert p.low_volume_nodes() == (), "an edge bin has one neighbour, not two"


def test_a_gap_is_not_a_node():
    """Prices never traded are not bins. A node is a price the session traded
    *through*, which means it traded there at all."""
    p = build([(100.0, 20), (105.0, 20)])
    assert p.low_volume_nodes() == ()


# --------------------------------------------------------------------------- #
# the derived forms a model consumes
# --------------------------------------------------------------------------- #
def test_distance_from_vpoc_is_signed_and_in_ticks():
    p = build([(100.0, 10)])
    assert p.distance_from_vpoc(100.5) == 2.0
    assert p.distance_from_vpoc(99.5) == -2.0


def test_value_area_position_is_below_inside_above():
    p = build([(100.0, 5), (100.25, 20), (100.5, 3), (100.75, 8), (101.0, 1)])
    low, high = p.value_area()
    assert p.value_area_position(low - 1) == -1
    assert p.value_area_position(low) == 0
    assert p.value_area_position(high) == 0
    assert p.value_area_position(high + 1) == 1


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [0, -0.25])
def test_a_nonpositive_tick_size_is_refused(bad):
    with pytest.raises(ValueError, match="tick_size"):
        SessionProfile(bad)


@pytest.mark.parametrize("bad", [0, -3])
def test_a_nonpositive_size_is_refused(bad):
    with pytest.raises(ValueError, match="size"):
        SessionProfile(0.25).add(100.0, bad)


def test_an_out_of_range_fraction_is_refused():
    p = build([(100.0, 5)])
    for bad in (0, 1.5, -0.1):
        with pytest.raises(ValueError, match="fraction"):
            p.value_area(bad)


# --------------------------------------------------------------------------- #
# add_bar: the construction the hypothesis registered, in one place
# --------------------------------------------------------------------------- #
def test_a_bar_spreads_its_volume_over_the_levels_it_touched():
    p = SessionProfile(0.25)
    p.add_bar(100.0, 100.5, 10)          # three levels
    assert {p.price_of(k): v for k, v in sorted(p._bins.items())} == {
        100.0: 4, 100.25: 3, 100.5: 3}


def test_volume_is_conserved_when_it_does_not_divide_evenly():
    """The remainder goes to the lowest levels rather than being dropped.
    `total_volume` has to equal what was actually traded, or the profile is
    describing a session that did not happen."""
    for volume in range(1, 40):
        p = SessionProfile(0.25)
        p.add_bar(100.0, 100.75, volume)   # four levels
        assert p.total_volume == volume, volume


def test_a_bar_that_never_moved_puts_everything_on_one_level():
    p = SessionProfile(0.25)
    p.add_bar(100.0, 100.0, 7)
    assert p.vpoc() == 100.0 and p.total_volume == 7


def test_bar_fed_and_tick_fed_profiles_agree_when_the_bar_did_not_move():
    """The approximation vanishes in the limit. A one-second NQ bar often spans
    a single level, which is why 1s reproduces the tick profile to about 3%."""
    bar = SessionProfile(0.25)
    tick = SessionProfile(0.25)
    for price, size in ((100.0, 5), (100.25, 9), (100.0, 2)):
        tick.add(price, size)
    bar.add_bar(100.0, 100.0, 7)          # the two 100.0 trades
    bar.add_bar(100.25, 100.25, 9)
    assert bar._bins == tick._bins


def test_add_bar_refuses_what_it_cannot_represent():
    p = SessionProfile(0.25)
    with pytest.raises(ValueError, match="volume"):
        p.add_bar(100.0, 100.5, 0)
    with pytest.raises(ValueError, match="below low"):
        p.add_bar(100.5, 100.0, 10)


def test_the_review_script_and_the_library_agree():
    """**Why this test exists.** The spreading rule was measured in
    `docs/reviews/task67/fidelity.py` and then moved here. If the two drifted,
    the resolution decision would have been made about a construction no
    strategy uses."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "t67", REPO_ROOT / "docs" / "reviews" / "task67" / "fidelity.py")
    t67 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t67)

    class FakeTick:
        def __init__(self, ts, price, size):
            self.ts, self.price, self.size = ts, price, size

    ticks = [FakeTick(0, 100.0, 5), FakeTick(10**8, 100.25, 9),
             FakeTick(2 * 10**8, 100.5, 3), FakeTick(10**9 + 1, 100.25, 7)]
    from_script = t67.bar_profile(ticks, 10**9, 0)

    mine = SessionProfile(0.25)
    mine.add_bar(100.0, 100.5, 17)      # first second: three levels, 17 contracts
    mine.add_bar(100.25, 100.25, 7)     # second second
    assert mine._bins == from_script._bins
