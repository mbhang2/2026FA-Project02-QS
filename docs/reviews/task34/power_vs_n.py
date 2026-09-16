#!/usr/bin/env python3
"""Item 2: detection probability as a function of campaign size N.

    python docs/reviews/task34/power_vs_n.py

Every additional proposal is another chance at an edge **and** another increment
of correction. Those pull opposite ways, so detection is not monotone in N and
there is an interior optimum. Nobody has asked what it is; the reference
literature runs as many trials as compute allows because nothing in those systems
gets harder as N grows. In a system with a correction, it does.

**Built from measured runs, not from a fitted distribution.** Task 33 produced a
library of real engine output on one data realization: 4,000 edgeless candidates
with their routes and nulls, and 600 candidates per planted strength with theirs.
A campaign of size N is assembled by drawing `pi*N` candidates from the
edge-carrying library and the rest from the edgeless one, and run through the
same `max_of_n` the gate uses. Nothing here is simulated except the composition
of the campaign.

**What that costs in independence, stated rather than buried.** Replicates are
drawn from finite libraries, so campaigns overlap. At N = 400 and pi = 0.01 a
campaign uses 396 of 4,000 edgeless candidates -- effectively independent. At
N = 400 and pi = 0.20 it uses 80 of 600 edge candidates, and two replicates
share about 13% of them on average. So the N = 400 cells at high pi are the
least independent in the grid and their intervals are optimistic; the trend
across N is what the design supports, not a precise value in any one cell.

**Sizing and what it resolves, before running.** B = 400 replicate campaigns per
cell. At a detection probability of 0.5 the binomial se is 0.025, so the 95%
half-width is 0.049: this resolves a 10-point difference in detection and cannot
resolve 3 points. That is enough to locate an interior optimum and to say whether
it is flat, and not enough to pin N* to a single grid value -- which is the
honest form of the answer anyway.

**Cells cut, deliberately.** `s = 0.24` -- the measured 80%-power point at
N = 10 -- is **not** run: it would need 600 fresh candidates plus their rotation
fallbacks, about 40 minutes of engine time, to place a point between two the
library already has. Given what the surface turns out to look like (monotone),
interpolating between s = 0.15 and s = 0.30 says everything a third row would.
That is the cut, and it is the engine work rather than a grid cell: every (pi, N)
combination runs, because each is a resample.

The N grid was **extended** past the brief's 400 to 800 and 1600, because the
first pass found the optimum sitting on the grid edge and an optimum at the edge
of a grid is not an optimum, it is a grid that stopped too early.
"""

from __future__ import annotations

import json
import math
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
T33 = REPO / "docs" / "reviews" / "task33"

import numpy as np  # noqa: E402

from src.core.scoring.correction import max_of_n  # noqa: E402

PI_GRID = (0.01, 0.05, 0.20)
S_GRID = (0.15, 0.30)          # 0.24 is interpolated; see the report
N_GRID = (10, 25, 50, 100, 200, 400, 800, 1600)
B, Q = 400, 0.05


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def load_library():
    """Scores and nulls for every candidate Task 33 ran, by planted strength."""
    pool = json.loads((T33 / "pool.json").read_text())
    buckets: dict[str, list[float]] = {}
    for r in pool:
        if r["score"] is not None:
            buckets.setdefault(r["bucket"], []).append(r["score"])
    usable = {k: np.asarray(v) for k, v in buckets.items() if len(v) >= 200}

    def unpack(doc, fallback_r):
        by_index = {}
        for slot, i in enumerate(doc["fallback_index"]):
            chunk = doc["rotation"][slot * fallback_r:(slot + 1) * fallback_r]
            by_index[i] = np.asarray([x for x in chunk if x is not None])
        out = []
        for j, (b, route) in enumerate(zip(doc["base"], doc["routes"])):
            if b["score"] is None or route is None:
                continue
            if route["route"] == "bucket":
                null = usable.get(route["bucket"])
            else:
                null = by_index.get(j)
            if null is None or null.size < 20:
                continue
            out.append((b["score"], null))
        return out

    edgeless = unpack(json.loads((T33 / "campaigns.json").read_text()), 50)
    power = json.loads((T33 / "power.json").read_text())
    edged = {float(k): unpack(v, 50) for k, v in power.items() if float(k) > 0}
    return edgeless, edged


def detect(rng, edgeless, edged, pi: float, n: int) -> tuple[bool, bool]:
    """One campaign. Returns (accepted anything, accepted a true-edge candidate)."""
    k = max(1, int(round(pi * n)))
    k = min(k, len(edged))
    idx_e = rng.choice(len(edged), size=k, replace=False)
    idx_0 = rng.choice(len(edgeless), size=n - k, replace=False)
    members = [(edged[i][0], edged[i][1], True) for i in idx_e]
    members += [(edgeless[i][0], edgeless[i][1], False) for i in idx_0]

    scores = [m[0] for m in members]
    best = int(np.argmax(scores))
    result = max_of_n([m[1] for m in members], scores[best], b=4_000, q=Q)
    return result["reject"], result["reject"] and members[best][2]


def main() -> int:
    edgeless, edged = load_library()
    print(f"library: {len(edgeless):,} edgeless candidates, "
          + ", ".join(f"{len(v)} at s={k}" for k, v in sorted(edged.items())))
    print(f"B={B} replicate campaigns per cell; se at 0.5 is "
          f"{math.sqrt(0.25 / B):.3f}, so the 95% half-width is "
          f"{1.96 * math.sqrt(0.25 / B):.3f}\n")

    rng = np.random.default_rng(3434)
    rows = []
    for s in S_GRID:
        lib = edged[s]
        print(f"=== planted strength s={s} ===")
        print(f"  {'pi':>6} " + "".join(f"{'N=' + str(n):>12}" for n in N_GRID))
        for pi in PI_GRID:
            cells, detail = [], {}
            for n in N_GRID:
                hits = sum(detect(rng, edgeless, lib, pi, n)[1] for _ in range(B))
                lo, hi = wilson(hits, B)
                detail[n] = {"detect": hits / B, "ci": [lo, hi]}
                cells.append(f"{hits / B:>12.3f}")
            best_n = max(detail, key=lambda n: detail[n]["detect"])
            peak = detail[best_n]["detect"]
            # "flat" = every N whose interval overlaps the peak's point estimate
            flat = [n for n in N_GRID if detail[n]["ci"][1] >= peak]
            rows.append({"s": s, "pi": pi, "cells": detail, "n_star": best_n,
                         "peak": peak, "flat_range": [min(flat), max(flat)]})
            print(f"  {pi:>6} " + "".join(cells)
                  + f"   N*={best_n}  flat {min(flat)}-{max(flat)}")
        print()

    print("FWER at each N, from the same machinery with pi = 0 (no edge planted):")
    print(f"  {'N':>6} {'FWER':>8} {'95% CI':>18}")
    fwer = []
    for n in N_GRID:
        hits = sum(detect(rng, edgeless, edged[S_GRID[0]], 0.0, n)[0]
                   for _ in range(B))
        # pi=0 still forces k>=1, so drop the edge member explicitly
        hits = 0
        for _ in range(B):
            idx = rng.choice(len(edgeless), size=n, replace=False)
            members = [edgeless[i] for i in idx]
            scores = [m[0] for m in members]
            hits += bool(max_of_n([m[1] for m in members], max(scores),
                                  b=4_000, q=Q)["reject"])
        lo, hi = wilson(hits, B)
        fwer.append({"n": n, "fwer": hits / B, "ci": [lo, hi]})
        print(f"  {n:>6} {hits / B:>8.3f} [{lo:.3f}, {hi:.3f}]")

    (pathlib.Path(__file__).parent / "power-vs-n.json").write_text(
        json.dumps({"b": B, "q": Q, "pi_grid": PI_GRID, "s_grid": S_GRID,
                    "n_grid": N_GRID, "rows": rows, "fwer_by_n": fwer}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
