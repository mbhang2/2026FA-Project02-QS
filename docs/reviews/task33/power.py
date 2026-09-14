#!/usr/bin/env python3
"""Item 3: the power curve. The measurement this project has never made.

    python docs/reviews/task33/power.py --stage pool
    python docs/reviews/task33/power.py --stage sweep
    python docs/reviews/task33/power.py --stage report

Thirty-three tasks of false-positive control and not one true-positive
measurement. Task 29 found a rule with **0% power** and found it by accident,
while implementing it for an unrelated reason. A gate that controls FWER
perfectly and accepts nothing is indistinguishable, from every number currently
recorded, from a gate that works.

**Design.** A campaign of N candidates on data carrying a planted signal of
strength s, using the synthetic generator's own `signal_strength` -- the same
machinery the Task 6/7 calibration used. The gate accepts if `max_of_n` rejects
the null for the campaign's best. Power at s is the fraction of campaigns where
it does.

**The nulls are always built on signal-free data.** A null tabulated on the
planted data would contain the signal and the test would have nothing to detect.
So the conditional profile table and the rotation fallbacks both run at
`signal_strength = 0.0` on the same seed, and only the campaign's own candidates
see the signal. That is what makes this a power measurement rather than a
tautology.

**Replication and resolvable effect, before running.** C = 60 campaigns per
strength, five strengths. At a true power of 0.8 the binomial se is 0.052, so the
95% half-width is **0.10**: this locates the 80%-power point to within roughly
one grid step and cannot distinguish 0.75 from 0.85. Five strengths x 60
campaigns x N = 10 is 3,000 base runs plus about 660 fallback candidates x 50
rotations -- roughly 2.5 hours at the 3.9 runs/s this machine sustains.

**What C = 60 does not support.** A claim like "power is exactly 0.8 at strength
s". What it does support is the shape of the curve, the strength band in which
power crosses 0.8, and whether the fallback's power is dramatically worse than
the bucket's -- which is the question the hybrid raises and the one that has to
be answered.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
HERE = pathlib.Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))
from hybrid_fwer import (  # noqa: E402
    DATASET_SEED, FALLBACK_R, WORK, _run, parallel, sources, wilson,
)

STRENGTHS = (0.0, 0.15, 0.3, 0.5, 0.8)
CAMPAIGNS, N_TRIALS = 60, 10


def stage_sweep(workers):
    from src.core.scoring.randomization import choose_null
    from src.core.scoring.randomization.profile_null import ProfileNull

    pool = json.loads((HERE / "pool.json").read_text())
    table = ProfileNull.from_runs([(r["returns"], r["score"]) for r in pool])

    out = {}
    for s in STRENGTHS:
        paths = sources(CAMPAIGNS * N_TRIALS, seed=34_000 + int(s * 1000),
                        prefix=f"pw{s}")
        print(f"strength {s}: {len(paths):,} candidates", flush=True)
        base = parallel([(p, None, s) for p in paths], workers, f"s={s}")
        routes = [choose_null(r["returns"], table) if r["score"] is not None
                  else None for r in base]
        fallback = [i for i, c in enumerate(routes)
                    if c and c.route == "rotation"]
        # the fallback null runs on SIGNAL-FREE data -- a null carrying the
        # planted signal would have nothing left to detect
        jobs = [(paths[i], 900_000 + 1_000 * i + k, 0.0)
                for i in fallback for k in range(FALLBACK_R)]
        rot = parallel(jobs, workers, f"s={s} rot") if jobs else []
        out[str(s)] = {
            "base": [{"score": r["score"], "returns": r["returns"]} for r in base],
            "routes": [c.as_dict() if c else None for c in routes],
            "fallback_index": fallback,
            "rotation": [r["score"] for r in rot],
        }
        (HERE / "power.json").write_text(json.dumps(out))
        print(f"  strength {s} done, rotation share "
              f"{len(fallback) / len(base):.1%}", flush=True)


def report():
    from src.core.scoring.correction import max_of_n
    from src.core.scoring.randomization.profile_null import ProfileNull

    pool = json.loads((HERE / "pool.json").read_text())
    table = ProfileNull.from_runs([(r["returns"], r["score"]) for r in pool])
    doc = json.loads((HERE / "power.json").read_text())

    print(f"power, C={CAMPAIGNS} campaigns per strength, N={N_TRIALS}, q=0.05")
    print(f"  se at power 0.8 is {math.sqrt(0.8 * 0.2 / CAMPAIGNS):.3f}; "
          f"the 95% half-width is {1.96 * math.sqrt(0.8 * 0.2 / CAMPAIGNS):.3f}\n")
    print(f"  {'strength':>9} {'power':>7} {'95% CI':>16} {'bucket':>8} "
          f"{'fallback':>9} {'rot share':>10} {'median best':>12}")
    rows = []
    for key in doc:
        d = doc[key]
        base, routes = d["base"], d["routes"]
        r_by_index = {}
        for slot, i in enumerate(d["fallback_index"]):
            chunk = d["rotation"][slot * FALLBACK_R:(slot + 1) * FALLBACK_R]
            r_by_index[i] = [x for x in chunk if x is not None]

        hits, hits_b, n_b, hits_r, n_r, bests, rot_n, tot = 0, 0, 0, 0, 0, [], 0, 0
        for c in range(CAMPAIGNS):
            samples, observed, kinds = [], [], []
            for j in range(c * N_TRIALS, (c + 1) * N_TRIALS):
                route = routes[j]
                if route is None or base[j]["score"] is None:
                    continue
                tot += 1
                if route["route"] == "bucket":
                    samples.append(table.buckets[route["bucket"]])
                    kinds.append("bucket")
                else:
                    rot_n += 1
                    sample = r_by_index.get(j, [])
                    if len(sample) < FALLBACK_R // 2:
                        continue
                    samples.append(sample)
                    kinds.append("rotation")
                observed.append(base[j]["score"])
            if not samples:
                continue
            best = max(observed)
            bests.append(best)
            kind = kinds[observed.index(best)]
            hit = bool(max_of_n(samples, best, b=10_000, q=0.05)["reject"])
            hits += hit
            if kind == "bucket":
                n_b += 1
                hits_b += hit
            else:
                n_r += 1
                hits_r += hit
        lo, hi = wilson(hits, CAMPAIGNS)
        med = sorted(bests)[len(bests) // 2] if bests else float("nan")
        rows.append({"strength": float(key), "power": hits / CAMPAIGNS,
                     "ci": [lo, hi], "power_bucket": hits_b / n_b if n_b else None,
                     "n_bucket": n_b,
                     "power_rotation": hits_r / n_r if n_r else None,
                     "n_rotation": n_r,
                     "rotation_share": rot_n / tot if tot else 0.0,
                     "median_best_sortino": med})
        print(f"  {float(key):>9.2f} {hits / CAMPAIGNS:>7.2f} "
              f"[{lo:.2f}, {hi:.2f}]".ljust(0)
              + f"{'':>2}{(hits_b / n_b if n_b else float('nan')):>8.2f} "
              f"{(hits_r / n_r if n_r else float('nan')):>9.2f} "
              f"{rot_n / tot if tot else 0:>10.1%} {med:>12.3f}")

    rows.sort(key=lambda r: r["strength"])
    crossing = None
    for a, b in zip(rows, rows[1:]):
        if a["power"] < 0.8 <= b["power"]:
            t = (0.8 - a["power"]) / (b["power"] - a["power"])
            crossing = a["strength"] + t * (b["strength"] - a["strength"])
            sortino = (a["median_best_sortino"]
                       + t * (b["median_best_sortino"] - a["median_best_sortino"]))
            break
    print()
    if crossing is None:
        print("  80% power is NOT reached anywhere on the swept grid.")
    else:
        print(f"  80% power at signal_strength ~ {crossing:.3f}, which is a "
              f"median best Sortino of ~{sortino:+.3f}")
    (HERE / "power-curve.json").write_text(json.dumps(
        {"campaigns": CAMPAIGNS, "n": N_TRIALS, "rows": rows,
         "strength_at_80": crossing}, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("sweep", "report"))
    ap.add_argument("--workers", type=int, default=max(os.cpu_count() - 2, 1))
    a = ap.parse_args()
    (stage_sweep if a.stage == "sweep" else lambda *_: report())(a.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
