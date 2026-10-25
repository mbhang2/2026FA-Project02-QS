"""The moving SPX surface: a Heston path, and options priced from its state.

A static snapshot has one arbitrage to find once, which does not test detection
speed at all. This is the version that moves -- remaining maturity shrinks,
variance wanders, and the plant's edge breathes with the spread around it.

The premise is unchanged and is still the first test: if the baseline contains
arbitrage nobody planted, nothing a campaign reports means anything.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from src.core.data.options import (                              # noqa: E402
    Plant, butterfly_violations, vertical_violations,
)
from src.core.data.options_spx import (                          # noqa: E402
    MATURITIES, SESSION_YEARS, THETA, V0, SPXHestonDataset, SPXHestonPath,
    SPXMovingSurface, SPXSpec,
)
from src.core.data.synthetic import SyntheticSpec                # noqa: E402
from src.core.engine import Engine                               # noqa: E402
from src.core.types import Split                                 # noqa: E402

FAST = dict(cos_terms=256, grid_points=601)


def _surface(plants=()):
    return SPXMovingSurface(SPXSpec(plants=plants, **FAST))


def _parity(surface, quotes, spot, elapsed):
    out = []
    for i in range(len(surface.maturities)):
        fwd, disc, _tau = surface.forward_discount(i, elapsed, spot)
        by: dict = {}
        for q in (x for x in quotes if x.expiry_session == i):
            by.setdefault(q.strike, {})[q.is_call] = q
        for K, pair in by.items():
            c, p = pair.get(True), pair.get(False)
            if not c or not p:
                continue
            fair = disc*(fwd - K)
            edge = max(fair - (c.ask - p.bid), (c.bid - p.ask) - fair)
            if edge > 1e-9:
                out.append({"maturity": i, "strike": K, "edge": edge})
    return out


# --------------------------------------------------------------------------- #
# the path
# --------------------------------------------------------------------------- #
def test_the_path_is_deterministic_and_the_variance_stays_valid():
    a = SPXHestonPath(n_sessions=5, seed=3)
    b = SPXHestonPath(n_sessions=5, seed=3)
    assert np.array_equal(a.spot, b.spot) and np.array_equal(a.var, b.var)
    assert SPXHestonPath(n_sessions=5, seed=4).spot[-1] != a.spot[-1]
    assert a.var.min() >= 0.0, "full truncation must keep the variance non-negative"
    assert a.var.max() < 1.0, "the variance process has exploded"


def test_the_path_realises_roughly_the_calibrated_vol():
    """Starting at v0 and reverting towards theta, so realised vol over a short
    path should sit near sqrt(v0) and below sqrt(theta)."""
    p = SPXHestonPath(n_sessions=20, seed=1)
    rv = float(np.std(np.diff(np.log(p.spot))))*math.sqrt(252*390)
    assert math.sqrt(V0)*0.6 < rv < math.sqrt(THETA)*1.2, (
        f"realised vol {rv:.4f} against sqrt(v0) {math.sqrt(V0):.4f} "
        f"and sqrt(theta) {math.sqrt(THETA):.4f}")


def test_the_path_is_one_simulation_not_one_per_session():
    """`SyntheticDataset` seeds per session because its sessions are
    independent. A Heston path is not -- v_t carries across the boundary -- so
    per-session seeding would reset the variance at every open."""
    p = SPXHestonPath(n_sessions=3, seed=7)
    opens = [p.var[k*390] for k in range(3)]
    assert len({round(v, 12) for v in opens}) == 3, (
        f"the variance restarts at each session open: {opens}")


# --------------------------------------------------------------------------- #
# the premise, along the whole path
# --------------------------------------------------------------------------- #
def test_the_moving_baseline_contains_no_arbitrage():
    s, p = _surface(), SPXHestonPath(n_sessions=10, seed=1)
    checked = 0
    # Twelve instants spread across ten sessions rather than a stride that
    # happened to give nine: the floor below is there to stop this test passing
    # on a surface that shrank, so the sampling has to earn it.
    for i in [int(x) for x in np.linspace(0, 10*390 - 1, 12)]:
        spot, var, elapsed = p.state(i)
        q = s.quotes_for_state(0, 0, spot, var, elapsed)
        checked += len(q)
        assert not _parity(s, q, spot, elapsed), f"parity violated at instant {i}"
        assert not vertical_violations(q), f"vertical violated at instant {i}"
        assert not butterfly_violations(q), f"butterfly violated at instant {i}"
    assert checked > 20000, f"only {checked} quotes checked"


def test_the_density_stays_a_valid_measure_as_the_state_moves():
    s, p = _surface(), SPXHestonPath(n_sessions=10, seed=2)
    for i in (0, 1500, 3000):
        _spot, var, elapsed = p.state(i)
        for m in MATURITIES:
            tau = m.T - elapsed
            rel, prob = s.density_for(tau, var)
            assert prob.min() >= 0.0
            assert abs(prob.sum() - 1.0) < 1e-9
            assert abs(float(rel @ prob) - 1.0) < 2e-3, (
                "E[S_T]/F drifted from one; the measure is not risk-neutral")


# --------------------------------------------------------------------------- #
# the plants
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("strength,expect", [(0.5, False), (8.0, True), (25.0, True)])
def test_a_plant_is_found_exactly_when_it_clears_the_spread(strength, expect):
    s = _surface(plants=(Plant("parity", 0, 0, strength),))
    p = SPXHestonPath(n_sessions=5, seed=1)
    hits, looks, edges = 0, 0, []
    for i in range(0, 5*390, 311):
        spot, var, elapsed = p.state(i)
        found = _parity(s, s.quotes_for_state(0, 0, spot, var, elapsed), spot, elapsed)
        looks += 1
        if found:
            hits += 1
            edges.append(max(r["edge"] for r in found))
    if not expect:
        assert hits == 0, f"a {strength} plant under the spread was reported {hits} times"
        return
    assert hits == looks, f"found at only {hits} of {looks} instants"
    assert max(edges) < strength, "the edge cannot exceed what was planted"
    assert max(edges) - min(edges) > 0.0, (
        "the edge must breathe as premium and variance move; a constant edge "
        "means the surface is not actually moving")


# --------------------------------------------------------------------------- #
# the dataset and the feed
# --------------------------------------------------------------------------- #
def test_a_run_may_not_outlive_the_front_expiry():
    """The calibration lists three expiries and has no mechanism for rolling
    into new ones. 60 sessions -- the parent's usual default -- reaches past the
    0.183-year front expiry, which is how the 40-session default was found."""
    with pytest.raises(ValueError, match="expiry"):
        SPXHestonDataset(spec=SyntheticSpec(n_sessions=60))
    d = SPXHestonDataset()
    assert d.spec.n_sessions*SESSION_YEARS < min(m.T for m in MATURITIES)


def test_the_dataset_declares_options_and_its_digest_covers_the_surface():
    clean = SPXHestonDataset(surface=_surface())
    planted = SPXHestonDataset(surface=_surface(plants=(Plant("parity", 0, 0, 20.0),)))
    assert clean.capabilities().has_options is True
    assert clean.digest() != planted.digest(), (
        "a planted run would be indistinguishable in the record from a clean one")
    assert SPXHestonDataset(path_seed=1).digest() != clean.digest()


def test_the_underlying_and_the_options_come_from_one_path():
    """An options feed driven by one series while the strategy trades another is
    a dataset that cannot be arbitraged correctly even in principle."""
    d = SPXHestonDataset()
    ticks = list(d.ticks(Split.TRAIN))
    assert ticks
    for t in ticks[:50]:
        idx = d._ts_index[t.ts]
        assert abs(d.path.spot[idx] - t.price) < 0.01, (
            "the tick price is not the path spot at that instant")


def test_the_feed_resolves_backwards_and_refuses_before_the_first_instant():
    """**Regression.** Exact-timestamp matching refused 144 of 144 reads on the
    first engine run, because a callback fires at a BAR CLOSE and not at a tick
    timestamp. Resolving to the latest instant at or before is also the correct
    meaning of "as of now"."""
    d = SPXHestonDataset()
    ticks = list(d.ticks(Split.TRAIN))
    t = ticks[10]
    exact = d.options().snapshot(t.ts, t.session, t.price)
    between = d.options().snapshot(t.ts + 1, t.session, t.price)
    assert [q.bid for q in exact] == [q.bid for q in between], (
        "a timestamp just after an instant must resolve to that instant")
    with pytest.raises(KeyError, match="not produced an instant"):
        d.options().snapshot(ticks[0].ts - 1, 0, t.price)


def test_the_parent_s_irregular_sessions_are_turned_off_deliberately():
    """The options state is indexed by position on a uniform minute grid, so
    ragged sessions would put the surface and the path on different clocks."""
    d = SPXHestonDataset(spec=SyntheticSpec(n_sessions=30, uniform=False))
    assert d.spec.uniform is True
    assert len(set(d._lengths)) == 1, f"session lengths are ragged: {set(d._lengths)}"


# --------------------------------------------------------------------------- #
# through the engine
# --------------------------------------------------------------------------- #
class _BoxSpotter:
    """**Model-free.** A box spread's payoff is deterministic, so buying one
    below zero or selling one above its width is arbitrage whatever r, q and tau
    are -- and a strategy cannot observe v_t, which is correct: implied vol is
    observable, the instantaneous variance of the process is not."""

    def setup(self, ctx):
        ctx.subscribe("30m")
        self.reads = self.hits = 0
        self.best = 0.0
        self.err = None

    def on_bars(self, ctx):
        try:
            self.reads += 1
            by: dict = {}
            for x in ctx.options():
                by.setdefault(x.expiry_session, {}).setdefault(x.strike, {})[x.is_call] = x
            worst = 0.0
            for strikes in by.values():
                ks = sorted(strikes)
                for a, b in zip(ks, ks[1:]):
                    pa, pb = strikes[a], strikes[b]
                    if len(pa) < 2 or len(pb) < 2:
                        continue
                    cost = (pa[True].ask - pa[False].bid) - (pb[True].bid - pb[False].ask)
                    proceeds = (pa[True].bid - pa[False].ask) - (pb[True].ask - pb[False].bid)
                    worst = max(worst, -cost, proceeds - (b - a))
            if worst > 1e-9:
                self.hits += 1
            self.best = max(self.best, worst)
        except Exception as exc:                                 # noqa: BLE001
            self.err = self.err or f"{type(exc).__name__}: {exc}"

    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass


def _run(plants):
    d = SPXHestonDataset(surface=_surface(plants=plants))
    st = _BoxSpotter()
    rec = Engine.for_test(d, st, split=Split.TRAIN, fill_model="unfavorable",
                          seed=5).run()
    return rec, st


def test_a_strategy_finds_the_plant_through_the_engine():
    rec, st = _run((Plant("parity", 0, 0, 25.0),))
    assert rec.outcome == "completed" and st.err is None, st.err
    assert st.reads > 100, f"only {st.reads} surface reads"
    assert st.hits == st.reads, f"found at {st.hits} of {st.reads} bars"
    assert st.best > 15.0, f"best edge {st.best} is far under the 25.0 planted"


def test_the_same_strategy_finds_nothing_on_a_clean_surface():
    rec, st = _run(())
    assert rec.outcome == "completed" and st.err is None, st.err
    assert st.reads > 100
    assert st.hits == 0, f"{st.hits} bars reported arbitrage on a clean surface"
