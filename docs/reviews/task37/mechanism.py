#!/usr/bin/env python3
"""What the planted signal is worth, and whether the null can see past it.

    python docs/reviews/task37/mechanism.py --stage sweep
    python docs/reviews/task37/mechanism.py --stage rotation

Run **before** the campaign, because both answers decide whether the campaign
can mean anything.

**The mechanism.** `SyntheticSpec.signal_strength` plants momentum over
consecutive `signal_interval_ticks = 10` tick blocks: the drift applied to block
*i* is `signal_strength * sign(return of block i-1)`, spread over the block. Tick
spacing is a median 30s, so **a 10-tick block is exactly one 5-minute bar** --
and `"5m"` is the interval every worked example in `strategy-api.md` uses. The
signal is therefore 5-minute bar momentum, which is the single most likely thing
for a model writing breakout and momentum code to land on. That is criterion 2
of the brief, and it is a property of the generator rather than a choice made to
flatter the result.

**Criterion 1, the theoretical best.** `ORACLE` below is the mechanism written
down as a strategy: on each 5m close, take a position in the direction of that
bar's return and hold exactly one bar. It knows the rule the generator uses, so
its score is the ceiling a candidate could reach -- no agent can do better
except by luck, and a real agent has to *find* the rule first.

**Criterion 3, the strengths.** Sized against the gate's own bar: Task 36
measured the best-of-100 null maximum at **+0.8011** at q = 0.05. One strength
is chosen clearly above it and one near it, from the sweep this script runs.

**And the question the brief did not ask, which has to be answered first.** The
gate's fallback null is a *circular rotation* of each session's price increments.
A circular rotation preserves the cyclic order of increments, so a relationship
between one block and the next survives everywhere except the single wrap seam.
If that is true here, the null contains the planted signal, a candidate that
exploits it scores just as well under the null, and **the positive control fails
for a reason that has nothing to do with the loop**. `--stage rotation` measures
it rather than reasoning about it.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics as st
import sys
import time
from concurrent.futures import ProcessPoolExecutor

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
HERE = pathlib.Path(__file__).resolve().parent
WORK = REPO / "runs" / "tmp-task37"

N_SESSIONS, TICKS, VOL = 175, 390, 0.15
DATASET_SEED = 1
SWEEP = (0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.75, 1.00)
ROTATION_R = 150
GATE_BAR_AT_N100 = 0.8011          # Task 36, max-of-N null at q = 0.05
WORKERS = 9

ORACLE = '''"""The planted mechanism, written down. Not a proposal -- apparatus.

The generator gives each 10-tick block a drift of `signal_strength * sign(the
previous block's return)`. Ticks are 30s apart, so a block is one 5m bar. This
takes a position in the direction of the last closed 5m bar and holds it for
exactly one bar, which is the rule the generator uses and therefore the ceiling.
"""

from __future__ import annotations


class Strategy:
    def setup(self, ctx) -> None:
        ctx.subscribe("5m")

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        bars = ctx.history("5m")
        if len(bars) < 1:
            return
        last = bars[-1]
        move = last.close - last.open
        if move == 0:
            return
        want = 1 if move > 0 else -1
        if ctx.position == want:
            return
        delta = want - ctx.position
        place = ctx.buy if delta > 0 else ctx.sell
        place(size=abs(delta), tag="oracle")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
'''


def _run(args):
    """One isolated run. Top level so it can cross a process pool."""
    path, strength, rot_seed, class_name = args
    sys.path.insert(0, str(REPO))
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.engine import Engine
    from src.core.engine.remote import RemoteStrategy
    from src.core.runlog import RunTicket
    from src.core.scoring.randomization.rotation import RotatedDataset
    from src.core.scoring.scorer import score
    from src.core.types import Split

    ds = SyntheticDataset(SyntheticSpec(
        n_sessions=N_SESSIONS, ticks_per_session=TICKS, vol_per_tick=VOL,
        seed=DATASET_SEED, signal_strength=strength))
    if rot_seed is not None:
        ds = RotatedDataset(ds, seed=rot_seed)
    ticket = RunTicket.for_null_calibration("task 37 mechanism calibration")
    try:
        with RemoteStrategy(pathlib.Path(path), class_name) as strategy:
            engine = Engine(ds, strategy, ticket=ticket, split=Split.TRAIN,
                            warmup_sessions=0, fill_model="unfavorable", seed=1)
            engine.run()
        blotter = engine.reconstructed()
        s = score(blotter, period=engine.session_calendar())
        return {"score": None if s.rejected else s.primary,
                "net_pnl": s.net_pnl, "trades": s.trade_count,
                "rejected": s.rejected, "reason": s.reject_reason}
    except Exception as exc:                       # pragma: no cover
        return {"score": None, "error": f"{type(exc).__name__}: {exc}"[:200]}


def _sources():
    WORK.mkdir(parents=True, exist_ok=True)
    oracle = WORK / "oracle.py"
    oracle.write_text(ORACLE)
    reference = REPO / "src" / "core" / "scoring" / "calibration" / "reference.py"
    return str(oracle), str(reference)


def stage_sweep() -> None:
    oracle, reference = _sources()
    jobs = ([(oracle, s, None, "Strategy") for s in SWEEP]
            + [(reference, s, None, "ReferenceStrategy") for s in SWEEP])
    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        out = list(pool.map(_run, jobs))
    oracle_rows = dict(zip(SWEEP, out[:len(SWEEP)]))
    ref_rows = dict(zip(SWEEP, out[len(SWEEP):]))

    print(f"105 training sessions, seed {DATASET_SEED}, unfavorable fills, "
          f"{time.monotonic() - t0:.0f}s")
    print(f"the gate's bar at N = 100 is the Task 36 null best-of-N: "
          f"{GATE_BAR_AT_N100:+.4f}\n")
    print(f"{'strength':>9} {'ORACLE':>9} {'trades':>7}   "
          f"{'reference':>9} {'trades':>7}   clears the bar?")
    for s in SWEEP:
        o, r = oracle_rows[s], ref_rows[s]
        os_, rs_ = o.get("score"), r.get("score")
        clears = "" if os_ is None else ("YES" if os_ > GATE_BAR_AT_N100 else "no")
        print(f"{s:>9.2f} {('  n/a' if os_ is None else f'{os_:+9.4f}')} "
              f"{o.get('trades', 0):>7}   "
              f"{('  n/a' if rs_ is None else f'{rs_:+9.4f}')} "
              f"{r.get('trades', 0):>7}   {clears}")

    (HERE / "sweep.json").write_text(json.dumps(
        {"sweep": list(SWEEP), "oracle": {str(k): v for k, v in oracle_rows.items()},
         "reference": {str(k): v for k, v in ref_rows.items()},
         "gate_bar_at_n100": GATE_BAR_AT_N100,
         "dataset_seed": DATASET_SEED, "n_sessions_train": 105}, indent=2) + "\n")
    print(f"\nwrote {HERE / 'sweep.json'}")


def stage_rotation(strengths) -> None:
    """Does the rotation null destroy the planted signal, or keep it?

    The oracle exploits the mechanism perfectly, so if its score survives
    rotation the null contains the signal. `R = 150` is Task 34's adopted
    resolution: the smallest p this can report is 1/151 = 0.0066, and the
    resampling bias at that R is inside measurement noise.
    """
    oracle, _ = _sources()
    results = {}
    for strength in strengths:
        observed = _run((oracle, strength, None, "Strategy"))
        jobs = [(oracle, strength, 80_000 + i, "Strategy")
                for i in range(ROTATION_R)]
        t0 = time.monotonic()
        with ProcessPoolExecutor(max_workers=WORKERS) as pool:
            rot = [r["score"] for r in pool.map(_run, jobs, chunksize=4)
                   if r.get("score") is not None]
        obs = observed["score"]
        exceed = sum(1 for v in rot if v >= obs)
        p = (1 + exceed) / (ROTATION_R + 1)
        results[str(strength)] = {
            "observed": obs, "r": ROTATION_R, "usable": len(rot),
            "p": p, "p_floor": 1 / (ROTATION_R + 1),
            "null_mean": st.fmean(rot), "null_sd": st.stdev(rot),
            "null_max": max(rot), "null_q05": sorted(rot)[int(0.95 * len(rot))],
            "wall_clock_s": round(time.monotonic() - t0, 1),
        }
        d = results[str(strength)]
        print(f"strength {strength:.2f}: oracle {obs:+.4f}   rotated null "
              f"mean {d['null_mean']:+.4f} sd {d['null_sd']:.4f} "
              f"max {d['null_max']:+.4f}   p = {p:.4f}"
              + ("   <- the null KEEPS the signal" if p > 0.05 else
                 "   <- the null destroys it"))
    (HERE / "rotation-check.json").write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nwrote {HERE / 'rotation-check.json'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True, choices=("sweep", "rotation"))
    ap.add_argument("--strengths", type=float, nargs="*", default=[0.30, 0.50])
    a = ap.parse_args()
    if a.stage == "sweep":
        stage_sweep()
    else:
        stage_rotation(a.strengths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
