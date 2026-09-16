#!/usr/bin/env python3
"""Read the three stages and report FWER under the conditional guarantee.

    python docs/reviews/task32/report.py

Both nulls on the same footing: each campaign's candidates get a p-value, the
campaign's best is tested against the simulated distribution of the maximum, and
the rate of campaigns with any rejection is the realized FWER.

The two nulls differ only in where a candidate's null sample comes from:

  profile    the disjoint calibration pool's scores in the candidate's bucket,
             all on THIS realization -- so it conditions on the data, which is
             the change from Task 31 and the whole point of the conditional
             framing.
  rotation   the candidate's own score under R rotations of this realization.
"""

from __future__ import annotations

import json
import math
import pathlib
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO))

from src.core.scoring.correction import correct, max_of_n  # noqa: E402
from src.core.scoring.randomization.profile_null import bucket_of  # noqa: E402

PROFILE_CAMPAIGNS, PROFILE_N = 400, 25
ROTATION_CAMPAIGNS, ROTATION_N, ROTATION_R = 40, 25, 30
MIN_BUCKET = 200


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson interval -- correct near 0, where the normal approximation is not."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def buckets_from_pool(rows) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in rows:
        if r["score"] is None:
            continue
        key = bucket_of(r["axes"]["active"], r["axes"]["downside"])
        out.setdefault(key, []).append(r["score"])
    return out


def campaign_fwer(campaigns, label: str, q: float = 0.05) -> dict:
    rejects, unusable, total, bh_hits = 0, 0, 0, 0
    for samples, observed, missing in campaigns:
        total += len(samples) + missing
        unusable += missing
        if not samples:
            continue
        best = max(observed)
        rejects += bool(max_of_n(samples, best, b=10_000, q=q)["reject"])
        ps = [ (1 + sum(1 for s in samp if s >= o)) / (len(samp) + 1)
               for samp, o in zip(samples, observed) ] + [1.0] * missing
        bh_hits += bool(correct(ps, q=q).bh_discoveries)
    c = len(campaigns)
    lo, hi = wilson(rejects, c)
    return {
        "label": label, "campaigns": c, "rejects": rejects,
        "fwer": rejects / c, "ci": [lo, hi],
        "controls": hi <= q or (lo <= q <= hi),
        "excludes_q": not (lo <= q <= hi),
        "bh_any": bh_hits / c,
        "unusable_fraction": unusable / total if total else 0.0,
    }


def report(q: float = 0.05) -> int:
    pool = json.loads((HERE / "pool.json").read_text())
    buckets = buckets_from_pool(pool)
    usable = {k: v for k, v in buckets.items() if len(v) >= MIN_BUCKET}
    print(f"conditional calibration, one data realization (seed 4242), q={q}")
    print(f"  calibration pool: {len(pool):,} candidates, "
          f"{sum(1 for r in pool if r['score'] is not None):,} scored")
    print(f"  buckets: {len(buckets)} total, {len(usable)} with >= {MIN_BUCKET} "
          f"({sum(len(v) for v in usable.values()):,} of "
          f"{sum(len(v) for v in buckets.values()):,} scores)")
    print()

    results = []
    prof_path = HERE / "profile-arm.json"
    if prof_path.exists():
        rows = json.loads(prof_path.read_text())
        campaigns = []
        for c in range(PROFILE_CAMPAIGNS):
            chunk = rows[c * PROFILE_N:(c + 1) * PROFILE_N]
            samples, observed, missing = [], [], 0
            for r in chunk:
                if r["score"] is None:
                    missing += 1
                    continue
                key = bucket_of(r["axes"]["active"], r["axes"]["downside"])
                sample = usable.get(key)
                if not sample:
                    missing += 1
                    continue
                samples.append(sample)
                observed.append(r["score"])
            campaigns.append((samples, observed, missing))
        results.append(campaign_fwer(campaigns, "profile (conditional)", q))

    rot_path = HERE / "rotation-arm.json"
    if rot_path.exists():
        rows = json.loads(rot_path.read_text())
        per = 1 + ROTATION_R
        campaigns = []
        for c in range(ROTATION_CAMPAIGNS):
            samples, observed, missing = [], [], 0
            for i in range(ROTATION_N):
                base = (c * ROTATION_N + i) * per
                head = rows[base]
                tail = [r["score"] for r in rows[base + 1:base + per]
                        if r["score"] is not None]
                if head["score"] is None or len(tail) < ROTATION_R // 2:
                    missing += 1
                    continue
                samples.append(tail)
                observed.append(head["score"])
            campaigns.append((samples, observed, missing))
        results.append(campaign_fwer(campaigns, f"rotation R={ROTATION_R}", q))

    print(f"  {'null':<24} {'C':>5} {'FWER':>7} {'95% CI':>18} "
          f"{'excludes q':>11} {'no null':>9}")
    for r in results:
        print(f"  {r['label']:<24} {r['campaigns']:>5} {r['fwer']:>7.3f} "
              f"[{r['ci'][0]:.3f}, {r['ci'][1]:.3f}]".rjust(0).ljust(0)
              + f"{'':>2}{str(r['excludes_q']):>11} "
              f"{r['unusable_fraction']:>8.1%}")
    print()
    for r in results:
        verdict = ("does NOT control" if r["excludes_q"] and r["fwer"] > q
                   else "consistent with control" if not r["excludes_q"]
                   else "conservative")
        print(f"  {r['label']}: {verdict}   (BH any: {r['bh_any']:.3f})")
    (HERE / "conditional-fwer.json").write_text(
        json.dumps({"q": q, "results": results,
                    "buckets_usable": len(usable)}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(report())
