#!/usr/bin/env python3
"""The validation Part B actually admits: are the p-values uniform under the null?

    python docs/reviews/task31/uniformity.py --candidates 60 --r 99

Part B asked whether the per-candidate null reproduces the committed table on the
reference strategy. It does not, and the reason is structural rather than a bug:

  the committed table   "what would this strategy score on a FRESH DRAW of the
                        market?" -- a bootstrap over sessions pooled from 120
                        independent runs, so it carries across-session variation.
  the rotation null     "what would this strategy score if its timing WITHIN each
                        session were destroyed?" -- conditional on the observed
                        sessions, whose net moves rotation preserves by design.

Those are nulls of different hypotheses and there is no reason for their
quantiles to agree. The rotation null is tighter (sd 0.068 against the table's
implied ~0.13) precisely because it holds fixed what the table resamples.

Conditioning is the point, not a defect: it is what makes the p-value robust to
the candidate's behavioural profile, which is the failure Task 30 measured. But
it means Part B's check cannot pass, and a check that cannot pass is not evidence
that anything is right.

**So the substitute, whose answer is also known in advance:** on the synthetic
generator every candidate is edgeless by construction, so a valid null must give
p-values that are Uniform(0, 1) across candidates and datasets. Measured with a
one-sample KS test and with the realized rate at each nominal level.

**Replication and resolvable effect.** `--candidates C` p-values, each from R
rotations. The KS statistic's 5% critical value is 1.36/sqrt(C), so C = 60
resolves a 0.176 departure in the CDF and no less; the realized rate at a nominal
alpha has binomial error sqrt(alpha(1-alpha)/C), so at alpha = 0.1 and C = 60
that is 0.039 -- this can tell 0.10 from 0.25 and cannot tell 0.10 from 0.15.
Levels below the floor 1/(R+1) are not testable at all and are reported as such.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.loop.agents import NullGenerator  # noqa: E402
from src.core.scoring.randomization import candidate_null  # noqa: E402
from src.core.types import Split  # noqa: E402

sys.path.insert(0, str(REPO / "docs" / "reviews" / "task30"))
from fdr_calibration import load_strategy  # noqa: E402


def ks_uniform(ps: list[float]) -> tuple[float, float]:
    """KS statistic against Uniform(0,1), and its 5% critical value."""
    n = len(ps)
    ordered = sorted(ps)
    d = max(max((i + 1) / n - p, p - i / n) for i, p in enumerate(ordered))
    return d, 1.36 / math.sqrt(n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", type=int, default=60)
    ap.add_argument("--r", type=int, default=99)
    args = ap.parse_args()

    agent = NullGenerator(seed=31)
    t0 = time.monotonic()
    ps, drifts, degenerate, floored = [], [], 0, 0
    for c in range(args.candidates):
        source = agent.propose([])
        cls = load_strategy(source, f"unif{c}")
        spec = SyntheticSpec(n_sessions=175, ticks_per_session=390,
                             vol_per_tick=0.15, seed=2000 + c)
        result = candidate_null(lambda cls=cls: cls(),
                                lambda spec=spec: SyntheticDataset(spec),
                                r=args.r, split=Split.TRAIN, warmup_sessions=0,
                                seed0=90_000 + 1000 * c)
        if result.degenerate:
            degenerate += 1
            continue
        ps.append(result.p)
        floored += int(result.at_floor)
        drifts.append(result.profile_drift)
        if (c + 1) % 10 == 0:
            print(f"  {c + 1}/{args.candidates} candidates, "
                  f"{time.monotonic() - t0:.0f}s", flush=True)

    n = len(ps)
    d, crit = ks_uniform(ps)
    print()
    print(f"{n} edgeless candidates, R={args.r} rotations each, "
          f"floor 1/(R+1) = {1 / (args.r + 1):.4f}")
    print(f"  degenerate nulls skipped   {degenerate}")
    print(f"  p-values at the floor      {floored}")
    print(f"  KS against Uniform(0,1)    D={d:.4f}   5% critical {crit:.4f}   "
          f"{'UNIFORM' if d <= crit else 'NOT UNIFORM'}")
    print()
    print(f"  {'nominal':>9} {'realized':>9} {'expected':>9} {'binomial se':>12}")
    for alpha in (0.05, 0.1, 0.2, 0.5):
        if alpha < 1 / (args.r + 1):
            print(f"  {alpha:>9} {'below floor':>9}")
            continue
        hit = sum(1 for p in ps if p <= alpha)
        se = math.sqrt(alpha * (1 - alpha) / n)
        print(f"  {alpha:>9} {hit / n:>9.4f} {alpha:>9} {se:>12.4f}"
              f"   {'ok' if abs(hit / n - alpha) <= 2 * se else 'OUTSIDE 2se'}")
    print()
    keys = sorted(drifts[0]) if drifts else []
    print("  median |profile drift| across candidates:")
    for key in keys:
        vals = sorted(abs(d[key]) for d in drifts)
        print(f"    {key:<24} median {vals[len(vals) // 2]:>7.1%}   "
              f"p90 {vals[int(len(vals) * 0.9)]:>7.1%}")

    (pathlib.Path(__file__).parent / "uniformity.json").write_text(
        json.dumps({"candidates": n, "r": args.r, "ks": d, "ks_critical": crit,
                    "p_values": ps, "degenerate": degenerate,
                    "at_floor": floored}, indent=1, sort_keys=True))
    print(f"\nwall {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
