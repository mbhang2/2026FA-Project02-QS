#!/usr/bin/env python3
"""Why max-of-N over the profile null is 2.3x liberal. A variance decomposition.

    python docs/reviews/task31/why_max_of_n_is_liberal.py

200 known-null campaigns put the max-of-N rejection rate at 0.115, CI [0.083,
0.177], which excludes 0.05. Better than Task 30's 0.575 by five times and still
not control, so the remaining cause is worth naming precisely.

**Correlation between trials is not it, and goes the other way.** Trials in a
campaign share one data realization, so they are positively correlated; the
maximum of correlated variables is *less* extreme than the maximum of independent
ones. Drawing each trial's null independently therefore overstates the spread and
is conservative.

**The candidate explanation is that the null is not conditional on the
realization.** A campaign's candidates all sit on one synthetic seed. If that
seed is favourable they score high *together*, and the observed best carries a
shared component. The profile bucket pools scores across 40 different seeds, so
independent draws from it cannot reproduce that shared component -- the observed
max is inflated by something the simulated max does not have.

That is testable directly: decompose the variance of each bucket's scores into a
between-seed part and a within-seed part. A large between-seed share is the
shared component, and it is exactly what a conditional null (rotation, which
holds the realization fixed) removes and a pooled null cannot.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.engine.remote import RemoteStrategy  # noqa: E402
from src.core.loop.agents import NullGenerator  # noqa: E402
from src.core.runlog import RunTicket  # noqa: E402
from src.core.scoring import metrics as metricsmod  # noqa: E402
from src.core.scoring.randomization.profile_null import (  # noqa: E402
    bucket_of, profile_axes,
)
from src.core.scoring.scorer import score  # noqa: E402
from src.core.types import Split  # noqa: E402


def main() -> int:
    strategies, seeds = 20, 15
    agent = NullGenerator(seed=777)
    tmp = REPO / "runs" / "tmp-decomp"
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(strategies):
        f = tmp / f"d{i}.py"
        f.write_text(agent.propose([]))
        paths.append(f)
    ticket = RunTicket.for_null_calibration("variance decomposition")

    t0 = time.monotonic()
    # scores[seed][strategy], and the bucket each landed in
    grid, buckets = {}, {}
    for si in range(seeds):
        spec = SyntheticSpec(n_sessions=175, ticks_per_session=390,
                             vol_per_tick=0.15, seed=6000 + si)
        for ci, path in enumerate(paths):
            with RemoteStrategy(path, "Strategy") as strategy:
                engine = Engine(SyntheticDataset(spec), strategy, ticket=ticket,
                                split=Split.TRAIN, warmup_sessions=0,
                                fill_model="unfavorable", seed=1)
                engine.run()
            blotter = engine.reconstructed()
            period = engine.session_calendar()
            returns = metricsmod.session_returns(blotter, period)
            s = score(blotter, period=period)
            if s.rejected:
                continue
            axes = profile_axes(returns)
            grid[(si, ci)] = s.primary
            buckets[(si, ci)] = bucket_of(axes["active"], axes["downside"])
        print(f"  seed {si + 1}/{seeds}, {time.monotonic() - t0:.0f}s", flush=True)

    print()
    print(f"{strategies} strategies x {seeds} seeds, "
          f"{len(grid)} scored runs")
    print()
    print("variance of the Sortino score, decomposed:")
    per_bucket = {}
    for (si, ci), value in grid.items():
        per_bucket.setdefault(buckets[(si, ci)], []).append((si, value))
    print(f"  {'bucket':>8} {'n':>5} {'total var':>10} {'between-seed':>13} "
          f"{'share':>7}")
    rows = []
    for bucket, items in sorted(per_bucket.items()):
        if len(items) < 30:
            continue
        values = [v for _, v in items]
        total = statistics.pvariance(values)
        by_seed = {}
        for si, v in items:
            by_seed.setdefault(si, []).append(v)
        means = [statistics.fmean(v) for v in by_seed.values() if len(v) >= 2]
        if len(means) < 3:
            continue
        between = statistics.pvariance(means)
        share = between / total if total else float("nan")
        rows.append({"bucket": bucket, "n": len(items), "total": total,
                     "between": between, "share": share})
        print(f"  {bucket:>8} {len(items):>5} {total:>10.5f} {between:>13.5f} "
              f"{share:>7.1%}")

    if rows:
        pooled = statistics.fmean([r["share"] for r in rows])
        print()
        print(f"  mean between-seed share of variance: {pooled:.1%}")
        print("  That share is the component a campaign's candidates hold in")
        print("  common and a pooled null cannot reproduce. A conditional null")
        print("  -- rotation, which holds the realization fixed -- removes it by")
        print("  construction, which is why its p-values came out uniform.")

    (pathlib.Path(__file__).parent / "variance-decomposition.json").write_text(
        json.dumps({"strategies": strategies, "seeds": seeds, "rows": rows},
                   indent=1, sort_keys=True))
    print(f"\nwall {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
