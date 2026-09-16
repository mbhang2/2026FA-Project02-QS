#!/usr/bin/env python3
"""Is there regime transfer at all? Ask distance, not a control.

    python docs/reviews/task28/transfer_by_distance.py --k 6

The same-window control in `transfer.py` does not work, and the reason is
structural rather than a coding slip: at build-window size S, the control builds
on S/2 sessions (half a window) and the cross pairs build on S. The control is
therefore always noisier than the thing it is meant to calibrate, and the
"transfer factor" it produces swings from 1.51 at K=4 to 0.22 at K=8 -- a number
that moves with a free parameter is not a measurement.

Making the control size-matched is not possible on 384 sessions: a no-transfer
control at build size S needs 2S sessions from one regime, and 2S contiguous
sessions *are* two adjacent windows, which is a cross pair.

So ask a question that needs no control. **If regime transfer drives the ratio,
it should grow with the distance between the build window and the check window.
If build-sample error drives it, distance should not matter.** Both quantities
are measured at identical build size, so the comparison is clean.

Everything is inside the 384-session training pool.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from transfer import N_GRID, measure, quantile  # noqa: E402

TABLE = REPO / "data" / "calibration" / "empirical-null-nq-trades.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--trials", type=int, default=100_000)
    args = ap.parse_args()

    doc = json.loads(TABLE.read_text())
    pool = np.asarray(doc["pool"], dtype=float)
    block = doc["block_length"]
    far_grid = tuple(doc["far_grid"])
    size = len(pool) // args.k
    windows = [pool[i * size:(i + 1) * size] for i in range(args.k)]

    print(f"K={args.k} windows of {size} sessions, block {block}, "
          f"{args.trials:,} trials per cell")
    print("ratio = realized FAR / promised, by |build window - check window|")
    print("all pairs share the same build size, so distance is the only variable\n")

    by_distance: dict[int, dict] = {}
    for i, build in enumerate(windows):
        for j, check in enumerate(windows):
            if i == j:
                continue
            got = measure(build, check, block_length=block, trials=args.trials,
                          far_grid=far_grid, seed=4242 + 10 * i + j)
            d = abs(i - j)
            for key, ratio in got.items():
                if ratio is not None:
                    by_distance.setdefault(d, {}).setdefault(key, []).append(ratio)
        print(f"  build window {i} done", flush=True)

    print()
    focus = [(86, 0.001), (86, 0.01), (73, 0.001), (73, 0.01), (100, 0.05)]
    head = f"{'distance':>9} {'pairs':>6} " + "".join(f"{f'n={n},far={f}':>16}"
                                                      for n, f in focus)
    print(head)
    print("-" * len(head))
    rows = {}
    for d in sorted(by_distance):
        cells = []
        npairs = len(next(iter(by_distance[d].values())))
        for key in focus:
            vals = by_distance[d].get(key, [])
            cells.append("               -" if not vals
                         else f"{quantile(vals, 0.5):>16.2f}")
        print(f"{d:>9} {npairs:>6} " + "".join(cells))
        rows[d] = {f"{n}|{f}": quantile(by_distance[d].get((n, f), [0]), 0.5)
                   for n, f in focus}

    print()
    print("pooled over the whole grid, median ratio by distance:")
    pooled = {}
    for d in sorted(by_distance):
        allv = [x for v in by_distance[d].values() for x in v]
        pooled[d] = quantile(allv, 0.5)
        print(f"  distance {d}: median {quantile(allv, 0.5):.2f}   "
              f"p90 {quantile(allv, 0.9):.2f}   n={len(allv)}")

    ds = sorted(pooled)
    if len(ds) > 1:
        slope = np.polyfit(ds, [pooled[d] for d in ds], 1)[0]
        print(f"\n  slope of median ratio against distance: {slope:+.3f} per window")
        print("  (a positive slope is regime transfer; a flat line is build-sample "
              "error\n   wearing a transfer costume)")

    (pathlib.Path(__file__).parent / f"distance-k{args.k}.json").write_text(
        json.dumps({"k": args.k, "window_sessions": size,
                    "trials": args.trials, "pooled_median": pooled,
                    "focus": rows}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
