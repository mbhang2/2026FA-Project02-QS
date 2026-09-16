#!/usr/bin/env python3
"""Item 1: characterize `max_of_n`'s resampling bias, and evaluate the remedies.

    python docs/reviews/task34/bias_grid.py

**The defect.** `max_of_n` resamples each trial's null with replacement, so the
simulated maximum can never exceed the largest draw that null happens to contain.
A finite null truncates its own upper tail, the simulated maximum comes out too
small, the observed best clears it too often, and the accept decision is biased
liberal. Task 33 measured two points; this fills in the grid and prices the fixes.

**Known nulls, so the answer is known.** Every campaign here is drawn from the
same standard normal as its nulls, so the target is exactly q and any excess is
the bias.

**Replication and resolvable effect, before running.** C = 4,000 campaigns per
cell. At the 0.05 target the binomial se is 0.0034, so the 95% half-width is
0.0068: this resolves a bias of 0.01 comfortably and can see 0.005. Below that it
cannot distinguish bias from noise, which matters because the tolerance a remedy
has to meet is of that order.

**Three remedies, each with a real cost:**

  raise R         no assumptions; cost is linear in engine runs, and the fallback
                  is already the expensive half of a campaign.
  subtract bias   free; assumes the bias measured on normal nulls transfers to
                  the real ones, which is an assumption about tail shape.
  smoothed tail   removes the cap by fitting the null's upper tail and sampling
                  beyond the observed maximum; costs a parametric assumption
                  exactly where the data is thinnest.
"""

from __future__ import annotations

import json
import math
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from src.core.scoring.correction import max_of_n  # noqa: E402

C, Q = 4_000, 0.05
R_GRID = (20, 30, 50, 75, 99, 150, 250, 500, 999, 2_000)
N_GRID = (10, 25, 50, 200)


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def measure(r: int, n: int, rng, b: int = 2_000) -> float:
    rejects = 0
    for _ in range(C):
        nulls = [rng.normal(0, 1, r) for _ in range(n)]
        best = float(rng.normal(0, 1, n).max())
        rejects += bool(max_of_n(nulls, best, b=b, q=Q)["reject"])
    return rejects / C


def main() -> int:
    rng = np.random.default_rng(34)
    se = math.sqrt(Q * (1 - Q) / C)
    print(f"{C:,} campaigns per cell, known standard-normal nulls, q={Q}")
    print(f"  binomial se {se:.4f}, 95% half-width {1.96 * se:.4f} -- resolves a "
          f"bias of 0.01 comfortably and can see 0.005\n")
    print(f"  {'R':>6} " + "".join(f"{'N=' + str(n):>16}" for n in N_GRID))
    rows = {}
    for r in R_GRID:
        cells = []
        for n in N_GRID:
            fwer = measure(r, n, rng)
            lo, hi = wilson(int(fwer * C), C)
            rows[f"{r}|{n}"] = {"r": r, "n": n, "fwer": fwer,
                                "bias": fwer - Q, "ci": [lo, hi]}
            cells.append(f"{fwer:>7.4f} ({fwer - Q:+.3f})")
        print(f"  {r:>6} " + "".join(f"{c:>16}" for c in cells))
    print()
    print("  cells are FWER (bias against q). The bias is the number a remedy")
    print("  has to remove.")
    (pathlib.Path(__file__).parent / "bias-grid.json").write_text(
        json.dumps({"campaigns": C, "q": Q, "cells": rows}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
