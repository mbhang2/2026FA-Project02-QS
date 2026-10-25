"""Multi-asset, stages 1-2: the asset dimension exists and costs nothing.

**The invariant these protect is "a single-asset run is byte-identical".** The
refactor is additive because `BlotterRow.as_row` is pinned by eight golden
cases and by every ledger row since Task 3, and because `ledger_check --replay`
re-executes 3,447 committed strategies against those digests. If one asset in
stops giving exactly the same tuple out, all of that is lost at once -- so it is
asserted here directly rather than inferred from the suite staying green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from src.core.engine import Engine                              # noqa: E402
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec   # noqa: E402
from src.core.types import (                                    # noqa: E402
    DEFAULT_ASSET, Blotter, BlotterRow, FillRecord, Side, Tick,
)


def _row(seq, asset=DEFAULT_ASSET, pnl=0.0, commission=0.1):
    return BlotterRow(seq=seq, ts=seq, order_id=seq, side=Side.BUY, price=1.5,
                      size=1, position_after=1, commission=commission,
                      realized_pnl=pnl, asset=asset)


# --------------------------------------------------------------------------- #
# the invariant
# --------------------------------------------------------------------------- #
def test_the_default_asset_adds_nothing_to_a_blotter_row():
    """One asset in, exactly the tuple that existed before assets did."""
    r = _row(1)
    assert len(r.as_row()) == 10, r.as_row()
    assert len(r.exact_row()) == 10
    assert r.as_row()[-1] == "", "the last field must still be the tag"


def test_a_named_asset_appends_exactly_one_field():
    r = _row(1, asset="SPX_C_7850_DEC26")
    assert len(r.as_row()) == 11
    assert r.as_row()[:10] == _row(1).as_row(), (
        "naming an asset must not disturb the fields in front of it")
    assert r.as_row()[-1] == "SPX_C_7850_DEC26"


def test_the_same_rule_holds_for_the_fill_log():
    """Replay compares the fill-log digest as well as the blotter digest, so
    both row builders need the rule or only half the record is stable."""
    base = dict(seq=1, ts=2, session=0, order_id=3, side=Side.BUY, size=1,
                price=1.5, liquidity="taker")
    assert len(FillRecord(**base).as_row()) == 9
    named = FillRecord(**base, asset="SPX_P_7800_DEC26")
    assert len(named.as_row()) == 10
    assert named.as_row()[:9] == FillRecord(**base).as_row()


def test_a_single_asset_blotter_digests_exactly_as_before():
    """The property the whole additive design exists to keep."""
    plain = Blotter()
    tagged = Blotter()
    for i in range(5):
        plain.add(_row(i))
        tagged.add(_row(i, asset=DEFAULT_ASSET))
    assert plain.digest() == tagged.digest()
    assert plain.exact_digest() == tagged.exact_digest()
    moved = Blotter()
    for i in range(5):
        moved.add(_row(i, asset="SPX_C"))
    assert moved.digest() != plain.digest(), (
        "a multi-asset run must be visibly a different thing")


def test_asset_is_the_last_field_on_tick_because_tick_is_built_positionally():
    """**Regression.** It went in before `side`, and `engine.py` builds
    `Tick(ts, price, size, session, seq, src.side)` with six positional
    arguments -- so the aggressor landed in `asset` and every order-flow feature
    saw an unknown side. Four tests caught it."""
    import dataclasses
    names = [f.name for f in dataclasses.fields(Tick)]
    assert names[-1] == "asset", names
    assert names.index("side") < names.index("asset")
    t = Tick(1, 2.0, 3, 4, 5, Side.BUY)
    assert t.side is Side.BUY and t.asset == DEFAULT_ASSET


# --------------------------------------------------------------------------- #
# the per-asset views
# --------------------------------------------------------------------------- #
def test_per_asset_figures_partition_the_total_exactly():
    b = Blotter()
    for i, (asset, pnl) in enumerate([(DEFAULT_ASSET, 1.0), ("SPX_C", 2.0),
                                      (DEFAULT_ASSET, 3.0), ("SPX_P", -0.5)]):
        b.add(_row(i, asset=asset, pnl=pnl))
    assert b.assets() == (DEFAULT_ASSET, "SPX_C", "SPX_P")
    assert b.realized_pnl_of(DEFAULT_ASSET) == pytest.approx(4.0)
    assert b.realized_pnl_of("SPX_C") == pytest.approx(2.0)
    assert sum(b.realized_pnl_of(a) for a in b.assets()) == pytest.approx(b.realized_pnl)
    assert sum(b.commission_of(a) for a in b.assets()) == pytest.approx(b.commission)
    assert len(b.rows_for("SPX_C")) == 1


def test_per_asset_views_are_filters_and_not_a_second_store():
    """`FillRecord`'s docstring is three paragraphs on why a score that reads
    accumulated state is only as trustworthy as that state. A parallel
    per-asset blotter would be a second place for the same number to live."""
    import inspect
    src = inspect.getsource(Blotter)
    assert "for r in self.rows if r.asset" in src
    assert "self.rows" in src and src.count("rows: list") == 1


# --------------------------------------------------------------------------- #
# the engine's books
# --------------------------------------------------------------------------- #
class _Noop:
    def setup(self, ctx): ctx.subscribe("5m")
    def on_bars(self, ctx): pass
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass


def _engine():
    from src.core.types import Split
    d = SyntheticDataset(SyntheticSpec(n_sessions=60, uniform=True))
    return Engine.for_test(d, _Noop(), split=Split.TRAIN,
                           fill_model="unfavorable", seed=1)


def test_the_default_book_is_the_run_state_itself_not_a_copy():
    """Aliased, so every existing path keeps mutating the object it always did
    and the port closure still sees six numbers. A synced copy would be two
    sources of truth for one number."""
    eng = _engine()
    assert eng._states[DEFAULT_ASSET] is eng._state
    eng._state.position = 7
    assert eng._states[DEFAULT_ASSET].position == 7
    assert eng.position_of(DEFAULT_ASSET) == 7


def test_a_new_book_starts_flat_and_inherits_the_session():
    eng = _engine()
    eng._state.session_start, eng._state.session_end = 111, 222
    book = eng._state_for("SPX_C_7850")
    assert book is not eng._state
    assert book.position == 0 and book.realized_pnl == 0.0
    assert (book.session_start, book.session_end) == (111, 222), (
        "an asset first traded mid-run must not be born with a session that "
        "never started")
    assert eng.position_of("never_traded") == 0


def test_assets_traded_lists_the_default_first():
    eng = _engine()
    eng._state_for("SPX_P_7800")
    eng._state_for("SPX_C_7850")
    assert eng.assets_traded() == (DEFAULT_ASSET, "SPX_C_7850", "SPX_P_7800")


def test_a_single_asset_run_opens_exactly_one_book():
    """Nothing in the engine should be creating books on its own."""
    eng = _engine()
    eng.run()
    assert eng.assets_traded() == (DEFAULT_ASSET,)
    assert eng.blotter.assets() in ((), (DEFAULT_ASSET,))


# --------------------------------------------------------------------------- #
# stage 3: the asset reaches a strategy
# --------------------------------------------------------------------------- #
def test_an_order_carries_its_asset_and_defaults_to_the_only_one():
    from src.core.types import Submit
    assert Submit(side=Side.BUY, size=1).asset == DEFAULT_ASSET
    import dataclasses
    assert [f.name for f in dataclasses.fields(Submit)][-1] == "asset", (
        "appended last, like every other asset field")


def test_the_order_allowlist_still_equals_the_signature():
    """A second copy of `_order`'s keyword-only parameters lives in the remote
    protocol, and the two drifted in both directions at once on 2026-10-14,
    costing 16 trials. Adding `asset` was the same drift starting again; the
    test that pins them is what stopped it."""
    import inspect
    from src.core.engine.context import BaseContext
    from src.core.engine.remote.protocol import ACTIONS
    kw = {n for n, p in inspect.signature(BaseContext._order).parameters.items()
          if p.kind is inspect.Parameter.KEYWORD_ONLY}
    assert "asset" in kw and "asset" in ACTIONS["buy"] and "asset" in ACTIONS["sell"]
    assert ACTIONS["buy"] - {"size"} <= kw, ACTIONS["buy"] - {"size"} - kw


def test_an_order_must_name_a_real_asset():
    """There is no "whichever book is open"."""
    from src.core.engine.context import ContextError
    import inspect
    src = inspect.getsource(BaseContextOrderGuard := __import__(
        "src.core.engine.context", fromlist=["x"]).BaseContext._order)
    assert "asset must be a non-empty name" in src
    assert ContextError is not None


class _TwoBooks:
    """Trades the default asset and reads a second book that was never opened."""

    def setup(self, ctx):
        ctx.subscribe("5m")
        self.seen = None
        self.err = None
        self.traded = False

    def on_bars(self, ctx):
        try:
            if not self.traded:
                ctx.buy(1)
                self.traded = True
            self.seen = (ctx.position, ctx.position_in(DEFAULT_ASSET),
                         ctx.position_in("SPX_C_7850"), ctx.assets_held())
        except Exception as exc:                                 # noqa: BLE001
            self.err = self.err or f"{type(exc).__name__}: {exc}"

    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass


def test_the_per_asset_readers_agree_with_the_old_ones_in_process():
    from src.core.types import Split
    d = SyntheticDataset(SyntheticSpec(n_sessions=60, uniform=True))
    st = _TwoBooks()
    rec = Engine.for_test(d, st, split=Split.TRAIN, fill_model="unfavorable",
                          seed=1).run()
    assert rec.outcome == "completed" and st.err is None, st.err
    pos, pos_default, pos_unopened, held = st.seen
    assert pos == pos_default, "ctx.position must be the default asset's book"
    assert pos_unopened == 0, "an unopened book is flat, not an error"
    assert held == (DEFAULT_ASSET,), held


def test_the_per_asset_readers_cross_the_process_boundary(tmp_path):
    """A scored strategy runs in a child, so the books have to travel. They are
    pushed -- three numbers an asset -- rather than pulled."""
    from src.core.engine.remote import RemoteStrategy
    from src.core.types import Split

    path = tmp_path / "strategy.py"
    path.write_text(
        "class Strategy:\n"
        "    def setup(self, ctx):\n"
        "        ctx.subscribe('5m'); self.n = 0\n"
        "    def on_bars(self, ctx):\n"
        "        if self.n == 0:\n"
        "            ctx.buy(1)\n"
        "        self.n += 1\n"
        "        assert ctx.position_in('primary') == ctx.position\n"
        "        assert ctx.position_in('never_opened') == 0\n"
        "        assert 'primary' in ctx.assets_held()\n"
        "    def on_fill(self, ctx): pass\n"
        "    def on_lifecycle(self, ctx): pass\n")
    d = SyntheticDataset(SyntheticSpec(n_sessions=60, uniform=True))
    with RemoteStrategy(path, "Strategy", sandbox=False) as remote:
        rec = Engine.for_test(d, remote, split=Split.TRAIN,
                              fill_model="unfavorable", seed=1).run()
    assert rec.outcome == "completed", f"{rec.outcome}: {rec.error}"
    assert rec.fills > 0, "the child never traded; the assertions proved nothing"
