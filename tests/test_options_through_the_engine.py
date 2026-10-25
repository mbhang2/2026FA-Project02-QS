"""`ctx.options()`: the surface reaches a strategy, and only what it should.

The surface itself is covered by `tests/test_options_surface.py`. These tests
are about the seam -- that a strategy can read the cross-section, that a dataset
without one says so by name, that the record admits which channel was
available, and that the sealed proxy did not get wider than one method.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from src.core.data.options import (                              # noqa: E402
    OptionsFeed, Plant, SyntheticOptionsDataset, SyntheticOptionsSpec,
    SyntheticOptionsSurface, parity_violations,
)
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec   # noqa: E402
from src.core.engine import Engine                               # noqa: E402
from src.core.engine.context import ContextError                 # noqa: E402
from src.core.types import Split                                 # noqa: E402

SMALL = dict(start_price=100.0, vol_per_session=1.0,
             near_halfwidth=8.0, wing_halfwidth=20.0)


def _dataset(plants=()):
    u = SyntheticDataset(SyntheticSpec(n_sessions=60, uniform=True))
    return SyntheticOptionsDataset(
        u, SyntheticOptionsSurface(SyntheticOptionsSpec(plants=plants, **SMALL)))


class _Spotter:
    """Reads the cross-section and counts parity violations. Trades nothing."""

    def setup(self, ctx):
        ctx.subscribe("5m")
        self.reads = self.hits = 0
        self.best = 0.0
        self.err = None
        self.forwards: list[tuple[float, float]] = []

    def on_bars(self, ctx):
        try:
            surface = ctx.options()
            self.reads += 1
            self.forwards.append((ctx.last_price, min(q.strike for q in surface)))
            v = parity_violations(list(surface), ctx.last_price)
            if v:
                self.hits += 1
                self.best = max(self.best, max(r["edge"] for r in v))
        except Exception as exc:                                 # noqa: BLE001
            self.err = self.err or f"{type(exc).__name__}: {exc}"

    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass


class _Blind:
    """Asks for options on a dataset that has none."""

    def setup(self, ctx):
        ctx.subscribe("5m")
        self.err = None

    def on_bars(self, ctx):
        try:
            ctx.options()
        except Exception as exc:                                 # noqa: BLE001
            self.err = self.err or exc

    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass


def _run(dataset, strategy):
    return Engine.for_test(dataset, strategy, split=Split.TRAIN,
                           fill_model="unfavorable", seed=3).run()


# --------------------------------------------------------------------------- #
# the seam works, and discriminates
# --------------------------------------------------------------------------- #
def test_a_strategy_reads_the_surface_and_finds_the_plant():
    d = _dataset(plants=(Plant("parity", 2, 0, 0.60),))
    st = _Spotter()
    rec = _run(d, st)
    assert rec.outcome == "completed" and st.err is None, st.err
    assert st.reads > 1000, f"only {st.reads} surface reads"
    assert st.hits == st.reads, (
        f"the plant is in every snapshot but only {st.hits} of {st.reads} bars saw it")
    assert st.best > 0.4, f"best edge {st.best} is far under the 0.60 planted"


def test_the_same_strategy_finds_nothing_on_a_clean_surface():
    """The discriminating half. Without this, the test above would pass on a
    checker that returns a violation for anything."""
    st = _Spotter()
    rec = _run(_dataset(), st)
    assert rec.outcome == "completed" and st.err is None, st.err
    assert st.reads > 1000
    assert st.hits == 0, f"{st.hits} bars reported arbitrage on a clean surface"


def test_the_surface_is_built_from_the_price_the_strategy_can_already_see():
    """**This is why `ctx.options()` needs no LookaheadError.** The forward is
    the last trade at or before the instant -- the number `ctx.last_price`
    returns -- so there is no future quote in the dataset to leak. The ladder
    must therefore be centred on that price and move with it."""
    st = _Spotter()
    _run(_dataset(), st)
    assert st.forwards
    centred = [abs(last - lowest) for last, lowest in st.forwards]
    assert max(centred) < 40.0, (
        "the ladder is not tracking the underlying the strategy sees")
    assert len({round(f, 2) for f, _ in st.forwards}) > 10, (
        "the forward never moved; the test proves nothing about tracking")


# --------------------------------------------------------------------------- #
# the refusals
# --------------------------------------------------------------------------- #
def test_a_dataset_without_options_says_so_by_name():
    """`ContextError` naming the capability, not an AttributeError from inside
    the engine and not an empty tuple that would read as "no quotes yet"."""
    st = _Blind()
    rec = _run(SyntheticDataset(SyntheticSpec(n_sessions=60, uniform=True)), st)
    assert rec.outcome == "completed"
    assert isinstance(st.err, ContextError), f"got {st.err!r}"
    assert "has_options" in str(st.err)


def test_the_record_says_which_channels_were_available():
    with_opts = _run(_dataset(), _Spotter())
    without = _run(SyntheticDataset(SyntheticSpec(n_sessions=60, uniform=True)),
                   _Spotter())
    assert with_opts.capabilities["has_options"] is True
    assert without.capabilities["has_options"] is False


def test_the_sealed_proxy_forwards_options_and_still_refuses_the_holdout():
    """An allowlist is the point of that proxy: the first options run failed
    with a bare `AttributeError: options` rather than quietly working, which is
    the behaviour to keep. Adding one channel must not widen anything else."""
    import src.core.engine.engine as eng_mod
    proxy_cls = None
    for obj in vars(eng_mod).values():
        if isinstance(obj, type) and getattr(obj, "_FORWARD", None):
            proxy_cls = obj
            break
    assert proxy_cls is not None, "could not find the sealed dataset proxy"
    assert "options" in proxy_cls._FORWARD
    sealed = proxy_cls(_dataset())
    assert callable(sealed.options)
    for forbidden in ("unlock_holdout", "holdout_lock", "_lock"):
        with pytest.raises(AttributeError):
            getattr(sealed, forbidden)


# --------------------------------------------------------------------------- #
# the dataset wrapper
# --------------------------------------------------------------------------- #
def test_the_digest_covers_the_surface_as_well_as_the_underlying():
    """Otherwise a run on three plants is indistinguishable in the record from
    a run on a clean surface."""
    u = SyntheticDataset(SyntheticSpec(n_sessions=60, uniform=True))
    clean = SyntheticOptionsDataset(u, SyntheticOptionsSurface(
        SyntheticOptionsSpec(**SMALL)))
    planted = SyntheticOptionsDataset(u, SyntheticOptionsSurface(
        SyntheticOptionsSpec(plants=(Plant("parity", 2, 0, 0.6),), **SMALL)))
    assert clean.digest() != planted.digest()
    assert clean.digest() != u.digest()


def test_the_wrapper_requires_a_split_like_the_protocol_does():
    """It defaulted `split=None`, which NQDataset tolerates and SyntheticDataset
    fails on deep inside `calendar.bounds` -- a signature neither honours."""
    import inspect
    sig = inspect.signature(SyntheticOptionsDataset.ticks)
    assert sig.parameters["split"].default is inspect.Parameter.empty


def test_an_optional_underlying_method_is_not_promised():
    """`sessions()` exists on NQDataset and not on SyntheticDataset, so naming
    it as an explicit delegate would promise a method that may not be there."""
    d = _dataset()
    with pytest.raises(AttributeError):
        d.sessions()
    assert hasattr(d, "unlock_holdout"), "the holdout lock must still forward"


def test_the_feed_caches_on_the_quantised_forward_and_restamps_the_instant():
    surface = SyntheticOptionsSurface(SyntheticOptionsSpec(**SMALL))
    feed = OptionsFeed(surface)
    a = feed.snapshot(1000, 0, 100.0)
    b = feed.snapshot(2000, 0, 100.004)        # same underlying tick
    assert feed.hits == 1 and feed.misses == 1
    assert [q.bid for q in a] == [q.bid for q in b]
    assert a[0].ts == 1000 and b[0].ts == 2000, "the instant must follow the read"


# --------------------------------------------------------------------------- #
# the remote boundary
# --------------------------------------------------------------------------- #
# A scored strategy does not run in this process -- it runs in a child, behind
# `RemoteStrategy`. So `ctx.options()` reaching a strategy means it crossing a
# pipe, and the surface is 882 rows: served as a PULL on request rather than
# pushed on every callback, so a strategy that never looks pays nothing.
# --------------------------------------------------------------------------- #
OPTIONS_STRATEGY = '''
from src.core.data.options import parity_violations

class Strategy:
    def setup(self, ctx):
        ctx.subscribe("5m")
        self.reads = 0
        self.hits = 0
        self.best = 0.0
        self.err = ""

    def on_bars(self, ctx):
        try:
            surface = ctx.options()
            self.reads += 1
            v = parity_violations(list(surface), ctx.last_price)
            if v:
                self.hits += 1
                self.best = max(self.best, max(r["edge"] for r in v))
            if self.reads == 1:
                ctx.buy(1)          # proves the callback still acts normally
        except Exception as exc:
            self.err = self.err or "%s: %s" % (type(exc).__name__, exc)

    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
'''


def test_the_surface_crosses_the_process_boundary(tmp_path):
    from src.core.engine.remote import RemoteStrategy

    path = tmp_path / "strategy.py"
    path.write_text(OPTIONS_STRATEGY)
    d = _dataset(plants=(Plant("parity", 2, 0, 0.60),))
    with RemoteStrategy(path, "Strategy", sandbox=False) as remote:
        rec = Engine.for_test(d, remote, split=Split.TRAIN,
                              fill_model="unfavorable", seed=3).run()
    assert rec.outcome == "completed", f"{rec.outcome}: {rec.error}"
    assert rec.capabilities["has_options"] is True
    # The child traded, so the callback was live and not merely surveyed.
    assert rec.fills > 0, "the remote strategy never filled; the run was inert"


def test_a_remote_strategy_that_never_looks_pays_for_nothing(tmp_path):
    """The reason `options` is a pull. A pushed collection would put 882 rows
    on every callback of every strategy, read or not."""
    import src.core.engine.remote._surface as surface
    pushed = {name for name, _enc in surface.BASECONTEXT_COLLECTIONS}
    assert "options" not in pushed, (
        "options became a pushed collection; every callback now carries 882 rows")
    assert "options" in surface.SURFACE["BaseContext"]


def test_the_child_refuses_options_without_a_pull_channel():
    """A RemoteContext built without a pull says so by name rather than failing
    with AttributeError on `self._pull`."""
    from src.core.engine.remote.child import RemoteContext, RemoteError

    ctx = RemoteContext({"scalars": {}}, [], {})
    with pytest.raises(RemoteError, match="pull channel"):
        ctx.options()
