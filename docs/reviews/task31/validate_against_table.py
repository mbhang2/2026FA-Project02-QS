#!/usr/bin/env python3
"""Part B: does the per-candidate null reproduce the committed table?

    python docs/reviews/task31/validate_against_table.py --r 200

The only free check available where the answer is known in advance. The committed
table was tabulated from the reference strategy's pooled session returns, so a
correct randomization *of that same strategy* should land on approximately the
same quantiles. Agreement means the machinery can be trusted on candidates whose
null nobody knows; disagreement means either the randomization or the table is
wrong and candidates cannot tell you which.

Validated against `empirical-null.json` (synthetic) rather than the NQ table.
Stated rather than glossed: the NQ version needs the reference strategy walked
over 384 real sessions R times, which is 278 s per rotation and about eight hours
at R = 100. The synthetic table is equally a committed table built from the same
reference strategy, and it is the one Task 30's failing campaign was scored
against, so it is the relevant comparison as well as the affordable one. The NQ
check is a background job, not an interactive one.

**Replication and resolvable effect.** R rotations put the p-value floor at
1/(R+1) and the sampling error of a tail quantile at roughly
sqrt(far(1-far)/R)/density. At R = 200 the far = 0.05 quantile has about 10
exceedances behind it, so this check can resolve a factor-of-two disagreement in
that quantile and cannot resolve 20%. Anything tighter needs more rotations, and
the far = 0.001 quantile is not resolvable at all here -- it is below the floor.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.scoring.calibration import null as nullmod  # noqa: E402
from src.core.scoring.randomization import candidate_null  # noqa: E402
from src.core.types import Split  # noqa: E402

SPEC = dict(n_sessions=175, ticks_per_session=390, vol_per_tick=0.15, seed=1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--r", type=int, default=200)
    ap.add_argument("--dataset", default="synthetic")
    args = ap.parse_args()

    result = candidate_null(
        ReferenceStrategy,
        lambda: SyntheticDataset(SyntheticSpec(**SPEC)),
        r=args.r, split=Split.TRAIN, warmup_sessions=0,
    )
    n_sessions = len(
        [x for x in result.null_scores if x == x])          # all of them
    del n_sessions

    print(f"reference strategy, {args.r} rotations, "
          f"{result.wall_clock_s:.0f}s "
          f"({result.wall_clock_s / (args.r + 1):.2f}s per run)")
    print(f"  observed score      {result.observed:+.4f}")
    print(f"  null mean           {statistics.fmean(result.null_scores):+.4f}")
    print(f"  null sd             {statistics.stdev(result.null_scores):.4f}")
    print(f"  p                   {result.p:.4f}   floor {result.p_floor:.4f}"
          f"   at floor: {result.at_floor}")
    print(f"  degenerate          {result.degenerate}")
    print()
    print("  behavioural profile, real data vs the median rotation:")
    obs = result.observed_profile.as_dict()
    drift = result.profile_drift
    for key, value in obs.items():
        print(f"    {key:<24} {value:>14.4g}   drift {drift[key]:+.1%}")
    print(f"  profile preserved (all within 25%): {result.profile_preserved}")
    print()

    doc = json.loads(
        (REPO / "data" / "calibration" / "empirical-null.json").read_text())
    entries = doc["table"]["sortino_per_session"]
    grid = sorted(int(k) for k in entries)
    # the split the rotations were scored on
    from src.core.data.dataset import sessions_for
    ds = SyntheticDataset(SyntheticSpec(**SPEC))
    _, live = sessions_for(ds.calendar(), Split.TRAIN, 0)
    n = min(grid, key=lambda g: abs(g - len(live)))
    print(f"  committed table, n={n} (nearest grid point to {len(live)} "
          f"training sessions)")
    print(f"    {'far':>8} {'table':>10} {'rotations':>11} {'ratio':>8}")
    rows = []
    worst = 0.0
    for far in sorted((float(k) for k in doc["far_grid"])):
        if far < result.p_floor:
            print(f"    {far:>8} {'-':>10} {'below floor':>11} {'-':>8}")
            continue
        table_q = entries[str(n)]["quantiles"][str(far)]
        rot_q = result.quantile(far)
        ratio = (rot_q / table_q) if table_q else float("nan")
        worst = max(worst, abs(rot_q - table_q))
        rows.append({"far": far, "table": table_q, "rotation": rot_q})
        print(f"    {far:>8} {table_q:>10.4f} {rot_q:>11.4f} {ratio:>8.2f}")
    print()
    print(f"  largest absolute disagreement over the resolvable grid: {worst:.4f}")
    print(f"  null sd for scale: {statistics.stdev(result.null_scores):.4f}   "
          f"-> {worst / statistics.stdev(result.null_scores):.2f} sd")

    (pathlib.Path(__file__).parent / "validate-against-table.json").write_text(
        json.dumps({"r": args.r, "n_grid_point": n,
                    "candidate": result.as_dict(), "rows": rows,
                    "worst_abs": worst}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
