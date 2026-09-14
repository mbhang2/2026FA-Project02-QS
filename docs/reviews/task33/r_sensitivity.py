#!/usr/bin/env python3
"""Why the rotation arm read 0.125 at R=30: `max_of_n` is liberal when R is small.

    python docs/reviews/task33/r_sensitivity.py

Task 32's side-by-side gave the rotation null FWER 0.125, CI [0.055, 0.261],
against the profile null's 0.045. Task 31 measured rotation p-values as uniform
(KS D = 0.1033 against a 5% critical 0.1756 at R = 99), so the two results are
only consistent if something about R = 30 is the problem rather than rotation.

It is, and it is structural. `max_of_n` simulates the maximum by resampling each
trial's null sample **with replacement**, so the simulated maximum can never
exceed the largest draw any trial's sample happens to contain. With N = 25 trials
whose nulls are 30 points each, the tail the maximum needs is simply not in the
data: the null's own upper tail is truncated at its sample maximum, the simulated
maximum is too small, the observed best clears it too often, and the test is
liberal.

This is a property of the correction, not of the null feeding it, and it is
measurable exactly: known standard-normal nulls, everything else identical, vary
R only.

**Replication and resolvable effect.** C = 2,000 campaigns per R. se at 0.05 is
0.0049, so the 95% half-width is 0.0096: this resolves 0.05 from 0.07 and
comfortably separates the R values from each other.
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

C, N, Q = 2_000, 25, 0.05


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def main() -> int:
    rng = np.random.default_rng(33)
    print(f"{C:,} campaigns x N={N} trials, standard normal nulls, q={Q}")
    print("the ONLY thing that varies is R, the size of each trial's null sample")
    print(f"  se at 0.05 is {math.sqrt(0.05 * 0.95 / C):.4f}; this resolves "
          f"0.05 from 0.07\n")
    print(f"  {'R':>6} {'floor 1/(R+1)':>14} {'FWER':>8} {'95% CI':>18}")
    rows = []
    for r in (30, 50, 99, 199, 499, 999):
        rejects = 0
        for _ in range(C):
            nulls = [rng.normal(0, 1, r) for _ in range(N)]
            best = float(max(rng.normal() for _ in range(N)))
            rejects += bool(max_of_n(nulls, best, b=2_000, q=Q)["reject"])
        lo, hi = wilson(rejects, C)
        rows.append({"r": r, "fwer": rejects / C, "ci": [lo, hi]})
        print(f"  {r:>6} {1 / (r + 1):>14.4f} {rejects / C:>8.3f} "
              f"[{lo:.3f}, {hi:.3f}]")
    print()
    print("  The observed best is drawn from the same standard normal as the")
    print("  nulls, so every campaign here is a known null and the target is")
    print(f"  {Q}. A rate above it is the correction being liberal, and the")
    print("  only cause available is the truncation of each null's upper tail.")
    (pathlib.Path(__file__).parent / "r-sensitivity.json").write_text(
        json.dumps({"campaigns": C, "n": N, "q": Q, "rows": rows}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
