#!/usr/bin/env python3
"""Part E: 40 known-null campaigns under the new null and the new correction.

    python docs/reviews/task31/acceptance.py --campaigns 40 --trials 50

Task 30's calibration, re-run with the two things this task changed:

- the null is **profile-indexed** rather than one strategy's, so a candidate is
  scored against edgeless strategies that trade like it does;
- the correction is the simulated **distribution of the best-of-N**, so the
  resolution needed is at the 5% level of that distribution rather than at
  `q/N` of a per-trial p-value column.

**Replication and resolvable effect, stated rather than eyeballed.** C campaigns
estimate the realized error rate with binomial error `sqrt(p(1-p)/C)`. At the
target p = 0.05 and C = 40 that is 0.034, so the 95% interval around an observed
rate spans about +/- 0.07: **this can distinguish 0.05 from 0.20 and cannot
distinguish 0.05 from 0.10.** Task 30's failure was 0.575, eleven times the
target and far outside that band, which is why 40 was enough to condemn it and
is not enough to certify a pass at 0.05 exactly. Read a pass here as "not broken
in the way Task 30 was", not as "controls at 0.05".

Both corrections are reported: BH over the profile-null p-values, with its
resolution cap stated, and the max-of-N test.
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
from src.core.engine import Engine  # noqa: E402
from src.core.loop.agents import NullGenerator  # noqa: E402
from src.core.scoring import metrics as metricsmod  # noqa: E402
from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.scoring.correction import correct, max_of_n, resolution_cap  # noqa: E402
from src.core.scoring.randomization.profile_null import ProfileNull  # noqa: E402
from src.core.scoring.scorer import score  # noqa: E402
from src.core.types import Split  # noqa: E402

sys.path.insert(0, str(REPO / "docs" / "reviews" / "task30"))
from fdr_calibration import load_strategy  # noqa: E402


def run_one(cls, spec):
    engine = Engine.for_test(SyntheticDataset(spec), cls(), split=Split.TRAIN,
                             warmup_sessions=0, fill_model="unfavorable", seed=1)
    engine.run()
    blotter = engine.reconstructed()
    period = engine.session_calendar()
    returns = metricsmod.session_returns(blotter, period)
    return score(blotter, period=period), returns


def campaign(table: ProfileNull, dataset_seed: int, trials: int, agent_seed: int,
             q: float) -> dict:
    agent = NullGenerator(seed=agent_seed)
    spec = SyntheticSpec(n_sessions=175, ticks_per_session=390,
                         vol_per_tick=0.15, seed=dataset_seed)
    ps, samples, observed, unusable = [], [], [], 0
    for i in range(trials):
        cls = load_strategy(agent.propose([]), f"acc{dataset_seed}_{i}")
        s, returns = run_one(cls, spec)
        if s.rejected:
            ps.append(1.0)
            continue
        look = table.lookup(returns, s.primary)
        if not look["usable"]:
            # No bucket, no p-value. Entering 1.0 keeps the trial in N without
            # inventing a number for it -- the alternative is dropping it, which
            # shrinks the denominator by exactly the trials the table cannot
            # speak about.
            unusable += 1
            ps.append(1.0)
            continue
        ps.append(look["p"])
        samples.append(table.sample_for(returns))
        observed.append(s.primary)

    bh = correct(ps, q=q)
    mx = (max_of_n(samples, max(observed), q=q, b=10_000)
          if observed else {"reject": False, "p": 1.0})
    return {
        "dataset_seed": dataset_seed, "n": len(ps),
        "unusable_bucket": unusable,
        "bh": bh.bh_discoveries, "holm": bh.holm_discoveries,
        "uncorrected": bh.uncorrected_discoveries,
        "max_of_n_p": mx["p"], "max_of_n_reject": mx["reject"],
        "min_p": min(ps),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaigns", type=int, default=40)
    ap.add_argument("--trials", type=int, default=50)
    ap.add_argument("--q", type=float, default=0.05)
    args = ap.parse_args()

    table = ProfileNull.read()
    t0 = time.monotonic()
    rows = []
    for c in range(args.campaigns):
        rows.append(campaign(table, 7000 + c, args.trials, agent_seed=900 + c,
                             q=args.q))
        if (c + 1) % 5 == 0:
            print(f"  {c + 1}/{args.campaigns}, "
                  f"maxN rejects {sum(r['max_of_n_reject'] for r in rows)}, "
                  f"BH hits {sum(1 for r in rows if r['bh'] > 0)}, "
                  f"{time.monotonic() - t0:.0f}s", flush=True)

    C = len(rows)
    se = math.sqrt(args.q * (1 - args.q) / C)
    maxn = sum(1 for r in rows if r["max_of_n_reject"])
    bh_any = sum(1 for r in rows if r["bh"] > 0)
    holm_any = sum(1 for r in rows if r["holm"] > 0)
    unc_any = sum(1 for r in rows if r["uncorrected"] > 0)
    total = sum(r["n"] for r in rows)
    unusable = sum(r["unusable_bucket"] for r in rows)

    print()
    print(f"{C} campaigns x {args.trials} edgeless trials = {total:,} trials, "
          f"q={args.q}")
    print(f"  binomial se at the target rate: {se:.4f}  -> a pass here means "
          f"'not broken like Task 30', not 'controls at {args.q}'")
    print(f"  candidates with no usable bucket    {unusable}/{total} = "
          f"{unusable / total:.1%}")
    print()
    print(f"  {'test':<34} {'rate':>8} {'vs q':>18}")
    for label, hits in (("max-of-N (FWER, the new test)", maxn),
                        ("BH over profile-null p", bh_any),
                        ("Holm over profile-null p", holm_any),
                        ("uncorrected", unc_any)):
        rate = hits / C
        within = abs(rate - args.q) <= 2 * se
        verdict = ("within 2se" if within else
                   ("ABOVE" if rate > args.q else "below (conservative)"))
        print(f"  {label:<34} {rate:>8.3f} {verdict:>18}   ({hits}/{C})")
    print()
    print(f"  BH's resolution cap at this table's floor: N <= "
          f"{resolution_cap(min(t['n'] for t in table.summary() if t['usable']), args.q)}"
          f"   (campaign N = {args.trials})")

    print()
    print("  the reference strategy against the same profile null, "
          "for the gap Task 30 measured:")
    ref_hits = 0
    for c in range(C):
        spec = SyntheticSpec(n_sessions=175, ticks_per_session=390,
                             vol_per_tick=0.15, seed=7000 + c)
        s, returns = run_one(ReferenceStrategy, spec)
        look = table.lookup(returns, s.primary)
        if look["usable"] and look["p"] <= 0.05:
            ref_hits += 1
    print(f"    reference at p <= 0.05   {ref_hits}/{C} = {ref_hits / C:.3f}")
    var_hits = sum(1 for r in rows for _ in [0] if r["min_p"] <= 0.05)
    del var_hits

    out = {"campaigns": C, "trials": args.trials, "q": args.q, "se": se,
           "max_of_n_rate": maxn / C, "bh_rate": bh_any / C,
           "holm_rate": holm_any / C, "uncorrected_rate": unc_any / C,
           "unusable_fraction": unusable / total,
           "reference_rate_at_0.05": ref_hits / C, "rows": rows}
    (pathlib.Path(__file__).parent / "acceptance.json").write_text(
        json.dumps(out, indent=1, sort_keys=True))
    print(f"\nwall {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
