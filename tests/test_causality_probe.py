"""The no-lookahead claim, measured rather than asserted.

**Where this comes from.** `Gowtam_L1/src/execution/leakage_guard.py` on
`origin/Gowtam-L1` has `assert_causal_vectorized`: perturb every bar at or after
a cutoff by a large offset, run the strategy again, and assert that positions
strictly before the cutoff are unchanged. It was written for a vectorised
strategy that is handed the whole series, where structural prevention cannot
apply. Task 45 named it the most interesting thing on that branch and said to
promote it. This is that promotion, adapted to an engine that never hands the
strategy a future bar in the first place.

**What it tests here.** This engine prevents lookahead *structurally*: bars are
delivered on close, `ctx.history` is a bounded window of closed bars, and the
strategy runs in a process that has no dataset in it. CLAUDE.md's rule is
"measure it; do not infer it", and the central safety claim of the repository
has until now been inferred from the design. So: run a strategy that records
**everything it can observe** through the context at every callback, on a
synthetic series and on the same series with every tick at or after a cutoff
moved by +100 points. Everything it observed before the cutoff must be
byte-identical, and every fill before the cutoff must be identical. If any
path -- a bar aggregator that peeks, a history view that reaches past `now`, a
session boundary computed from the future, a fill model pricing off the next
tick -- lets the perturbation reach the strategy early, the observation logs
diverge and the test names the first callback where they did.

**The positive control.** A test like this passes vacuously if the perturbation
never reaches the strategy at all. So the logs *after* the cutoff are asserted
to differ. Both halves are required: identical before, different after.
"""
from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.types import Split, Tick  # noqa: E402

OFFSET = 100.0


def _dataset(**kw) -> SyntheticDataset:
    base = dict(n_sessions=40, ticks_per_session=390, vol_per_tick=0.15, seed=1,
                train_frac=0.5, validation_frac=0.2)
    base.update(kw)
    return SyntheticDataset(SyntheticSpec(**base))


class Perturbed:
    """The same dataset with every tick at or after `cutoff_ns` moved by
    `OFFSET`. Everything else is delegated, so the engine sees one `Dataset`."""

    def __init__(self, inner: SyntheticDataset, cutoff_ns: int):
        self._inner = inner
        self.cutoff_ns = cutoff_ns
        self.name = inner.name + ":perturbed"

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def ticks(self, split: Split, *, warmup: int = 0) -> Iterator[Tick]:
        for t in self._inner.ticks(split, warmup=warmup):
            if t.ts >= self.cutoff_ns:
                yield Tick(ts=t.ts, price=t.price + OFFSET, size=t.size,
                           session=t.session, seq=t.seq, side=t.side)
            else:
                yield t


class Spy:
    """Trades like the committed momentum strategy, so there are fills to
    compare, and writes down everything the context will tell it."""

    interval = "5m"

    def __init__(self):
        self.log: list[tuple] = []

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def _observe(self, kind: str, ctx) -> None:
        hist = ctx.history(self.interval)
        tail = tuple((b.open_ts, b.close_ts, b.open, b.high, b.low, b.close,
                      b.volume) for b in hist[-3:]) if len(hist) else ()
        closed = tuple((b.sub_id, b.open_ts, b.close_ts, b.open, b.close)
                       for b in ctx.closed)
        fills = tuple((f.ts, f.price, f.size, str(f.side))
                      for f in getattr(ctx, "fills", ()))
        self.log.append((
            kind, ctx.ts, ctx.session, ctx.session_start, ctx.session_end,
            ctx.position, ctx.last_price, round(ctx.equity, 9),
            round(ctx.realized_pnl, 9), round(ctx.unrealized_pnl, 9),
            len(ctx.working_order_ids), closed, tail, fills, ctx.actionable,
        ))

    def on_bars(self, ctx):
        self._observe("bars", ctx)
        if ctx.position or ctx.working_order_ids:
            return
        hist = ctx.history(self.interval)
        if len(hist) < 1:
            return
        last = hist[-1]
        if last.close == last.open:
            return
        place = ctx.buy if last.close > last.open else ctx.sell
        place(size=1, stop_offset=4.0, target_offset=4.0, tag="entry")

    def on_fill(self, ctx):
        self._observe("fill", ctx)

    def on_lifecycle(self, ctx):
        self._observe("lifecycle", ctx)


def _run(dataset):
    spy = Spy()
    eng = Engine.for_test(dataset, spy, split=Split.VALIDATION)
    eng.run()
    return spy.log, [asdict(f) for f in eng.fill_log]


def _cutoff(ds: SyntheticDataset) -> int:
    """The open of a session in the middle of the validation split, so there
    is history on both sides of it."""
    from src.core.data.dataset import sessions_for
    _, live = sessions_for(ds.calendar(), Split.VALIDATION, 0)
    live = list(live)
    mid = live[len(live) // 2]
    start, _ = ds.session_bounds(mid)
    return start


def test_nothing_the_strategy_can_observe_before_the_cutoff_depends_on_data_after_it():
    base_ds = _dataset()
    cutoff = _cutoff(base_ds)
    base_log, base_fills = _run(base_ds)
    pert_log, pert_fills = _run(Perturbed(_dataset(), cutoff))

    before = [e for e in base_log if e[1] < cutoff]
    before_p = [e for e in pert_log if e[1] < cutoff]
    assert before, "no observations before the cutoff; the probe measured nothing"
    for i, (a, b) in enumerate(zip(before, before_p)):
        assert a == b, (
            f"observation #{i} ({a[0]} at ts={a[1]}) differs between the base "
            f"and the perturbed run, before the cutoff at {cutoff}: the "
            f"strategy could see data from after the cutoff.\n  base: {a}\n  "
            f"perturbed: {b}")
    assert len(before) == len(before_p), (
        f"{len(before)} observations before the cutoff on the base run, "
        f"{len(before_p)} on the perturbed one")

    fb = [f for f in base_fills if f["ts"] < cutoff]
    fp = [f for f in pert_fills if f["ts"] < cutoff]
    assert fb, "no fills before the cutoff; the fill path was not exercised"
    assert fb == fp, ("a fill before the cutoff differs: the fill model or the "
                      "order path saw data from after it")


def test_the_perturbation_actually_reaches_the_strategy_after_the_cutoff():
    """The positive control. Without it the test above passes for a dataset
    wrapper that perturbs nothing."""
    base_ds = _dataset()
    cutoff = _cutoff(base_ds)
    base_log, _ = _run(base_ds)
    pert_log, _ = _run(Perturbed(_dataset(), cutoff))
    after = [e for e in base_log if e[1] >= cutoff]
    after_p = [e for e in pert_log if e[1] >= cutoff]
    assert after and after_p
    prices = [e[6] for e in after if e[6]]
    prices_p = [e[6] for e in after_p if e[6]]
    assert prices and prices_p
    # Every observed last price after the cutoff moved by the offset; on the
    # first shared callback that is exact.
    assert abs((prices_p[0] - prices[0]) - OFFSET) < 1e-9, (prices[0], prices_p[0])
    assert after != after_p


def test_the_cutoff_sits_inside_the_split_with_history_on_both_sides():
    ds = _dataset()
    cutoff = _cutoff(ds)
    log, _ = _run(ds)
    n_before = sum(1 for e in log if e[1] < cutoff)
    n_after = sum(1 for e in log if e[1] >= cutoff)
    assert n_before > 50 and n_after > 50, (n_before, n_after)
