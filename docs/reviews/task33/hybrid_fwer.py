#!/usr/bin/env python3
"""Task 33 items 1-2: FWER of the hybrid null, at a size the claim can carry.

    python docs/reviews/task33/hybrid_fwer.py --stage pool
    python docs/reviews/task33/hybrid_fwer.py --stage campaigns
    python docs/reviews/task33/hybrid_fwer.py --stage report

**The hybrid.** Bucket when the candidate's profile bucket holds >= 200 scores,
rotation when it does not. The routing keys on the profile and nothing else and
is fixed before the campaign runs -- `choose_null` is never given a score,
because a function that cannot see the outcome cannot select on it.

**Expect FWER to rise.** Task 32's 0.045 was conservative *because* 21.8% of
candidates were auto-rejected at p = 1. Giving them a real p-value is the fix and
the rise is the honest number.

**Sizing, and the claim it can carry, stated before running.** The brief asks for
an upper CI bound below 0.06, which at q = 0.05 needs C ~ 1,900 campaigns. With
the hybrid's rotation share that is 1,900 x 10 x 0.218 x 51 = 211,000 isolated
runs on top of 19,000 base runs: **16 hours at the 4 runs/s this machine
sustains on eight workers.** Not affordable here.

So this runs at **C = 400, N = 10, fallback R = 50**, which is about 3.4 hours,
and the claim it supports is stated rather than dressed up:

    se at 0.05 is 0.0109, Wilson upper bound near 0.073.
    AFFORDABLE CLAIM:  "the hybrid's FWER upper bound is below about 0.075".
    NOT AFFORDABLE:    "below 0.06", which needs C ~ 1,900 and ~16 hours.

`R = 50` rather than 99 costs a measured +0.009 of liberalism in the correction
(`r_sensitivity.py`), which is an upward bias on the estimate and is stated
rather than absorbed.
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
WORK = REPO / "runs" / "tmp-task33"

DATASET_SEED = 4_242
CAMPAIGNS, N_TRIALS, FALLBACK_R = 400, 10, 50
CALIB_POOL = 1_500


def _run(args):
    """One isolated run. Top level so it can cross a process pool."""
    path, rot_seed, signal = args
    sys.path.insert(0, str(REPO))
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.engine import Engine
    from src.core.engine.remote import RemoteStrategy
    from src.core.runlog import RunTicket
    from src.core.scoring import metrics as metricsmod
    from src.core.scoring.randomization.rotation import RotatedDataset
    from src.core.scoring.scorer import score
    from src.core.types import Split

    ds = SyntheticDataset(SyntheticSpec(
        n_sessions=175, ticks_per_session=390, vol_per_tick=0.15,
        seed=DATASET_SEED, signal_strength=signal))
    if rot_seed is not None:
        ds = RotatedDataset(ds, seed=rot_seed)
    ticket = RunTicket.for_null_calibration("task 33 hybrid calibration")
    with RemoteStrategy(pathlib.Path(path), "Strategy") as strategy:
        engine = Engine(ds, strategy, ticket=ticket, split=Split.TRAIN,
                        warmup_sessions=0, fill_model="unfavorable", seed=1)
        engine.run()
    blotter = engine.reconstructed()
    period = engine.session_calendar()
    returns = metricsmod.session_returns(blotter, period)
    s = score(blotter, period=period)
    return {"score": None if s.rejected else s.primary, "returns": returns}


def sources(n: int, seed: int, prefix: str) -> list[str]:
    from src.core.loop.agents import NullGenerator

    WORK.mkdir(parents=True, exist_ok=True)
    agent = NullGenerator(seed=seed)
    out = []
    for i in range(n):
        f = WORK / f"{prefix}{i}.py"
        f.write_text(agent.propose([]))
        out.append(str(f))
    return out


def parallel(jobs, workers, label):
    t0 = time.monotonic()
    out = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for i, r in enumerate(pool.map(_run, jobs, chunksize=4), start=1):
            out.append(r)
            if i % 1000 == 0:
                rate = i / (time.monotonic() - t0)
                print(f"  {label}: {i:,}/{len(jobs):,}  {rate:.1f}/s  "
                      f"eta {(len(jobs) - i) / rate / 60:.0f} min", flush=True)
    return out


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def stage_pool(workers):
    paths = sources(CALIB_POOL, seed=33_001, prefix="cal")
    rows = parallel([(p, None, 0.0) for p in paths], workers, "pool")
    (HERE / "pool.json").write_text(json.dumps(rows))
    print(f"  {sum(1 for r in rows if r['score'] is not None):,} scored")


def stage_campaigns(workers):
    from src.core.scoring.randomization import choose_null
    from src.core.scoring.randomization.profile_null import ProfileNull

    pool = json.loads((HERE / "pool.json").read_text())
    table = ProfileNull.from_runs([(r["returns"], r["score"]) for r in pool])
    usable = [k for k, v in table.buckets.items() if len(v) >= 200]
    print(f"conditional table: {len(table.buckets)} buckets, {len(usable)} usable")

    paths = sources(CAMPAIGNS * N_TRIALS, seed=33_002, prefix="camp")
    base = parallel([(p, None, 0.0) for p in paths], workers, "base")

    routes = [choose_null(r["returns"], table) if r["score"] is not None else None
              for r in base]
    fallback = [i for i, c in enumerate(routes) if c and c.route == "rotation"]
    share = len(fallback) / len(base)
    print(f"  rotation share: {len(fallback):,}/{len(base):,} = {share:.1%}   "
          f"-> {len(fallback) * FALLBACK_R:,} more runs", flush=True)

    jobs = [(paths[i], 800_000 + 1_000 * i + k, 0.0)
            for i in fallback for k in range(FALLBACK_R)]
    rot = parallel(jobs, workers, "rotation")
    (HERE / "campaigns.json").write_text(json.dumps({
        "base": [{"score": r["score"], "returns": r["returns"]} for r in base],
        "routes": [c.as_dict() if c else None for c in routes],
        "fallback_index": fallback,
        "rotation": [r["score"] for r in rot],
        "fallback_r": FALLBACK_R,
    }))


def report():
    from src.core.scoring.correction import max_of_n
    from src.core.scoring.randomization.profile_null import ProfileNull

    pool = json.loads((HERE / "pool.json").read_text())
    table = ProfileNull.from_runs([(r["returns"], r["score"]) for r in pool])
    doc = json.loads((HERE / "campaigns.json").read_text())
    base, routes = doc["base"], doc["routes"]
    r_by_index = {}
    for slot, i in enumerate(doc["fallback_index"]):
        chunk = doc["rotation"][slot * doc["fallback_r"]:(slot + 1) * doc["fallback_r"]]
        r_by_index[i] = [x for x in chunk if x is not None]

    rejects, used_rot, used_bucket, missing = 0, 0, 0, 0
    for c in range(CAMPAIGNS):
        samples, observed = [], []
        for j in range(c * N_TRIALS, (c + 1) * N_TRIALS):
            route = routes[j]
            if route is None or base[j]["score"] is None:
                missing += 1
                continue
            if route["route"] == "bucket":
                samples.append(table.buckets[route["bucket"]])
                used_bucket += 1
            else:
                sample = r_by_index.get(j, [])
                if len(sample) < doc["fallback_r"] // 2:
                    missing += 1
                    continue
                samples.append(sample)
                used_rot += 1
            observed.append(base[j]["score"])
        if samples:
            rejects += bool(max_of_n(samples, max(observed), b=10_000, q=0.05)["reject"])

    lo, hi = wilson(rejects, CAMPAIGNS)
    total = used_bucket + used_rot + missing
    print(f"hybrid, C={CAMPAIGNS} campaigns x N={N_TRIALS}, fallback R={doc['fallback_r']}")
    print(f"  routed to a bucket    {used_bucket:,}/{total:,} = {used_bucket / total:.1%}")
    print(f"  routed to rotation    {used_rot:,}/{total:,} = {used_rot / total:.1%}")
    print(f"  no null at all        {missing:,}/{total:,} = {missing / total:.1%}")
    print(f"  FWER  {rejects / CAMPAIGNS:.3f}   95% CI [{lo:.3f}, {hi:.3f}]")
    print(f"  Task 32, profile only: 0.045 [0.029, 0.070] with 21.8% auto-rejected")
    (HERE / "hybrid-fwer.json").write_text(json.dumps({
        "campaigns": CAMPAIGNS, "n": N_TRIALS, "fallback_r": doc["fallback_r"],
        "fwer": rejects / CAMPAIGNS, "ci": [lo, hi],
        "bucket_share": used_bucket / total, "rotation_share": used_rot / total,
        "missing_share": missing / total}, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                    choices=("pool", "campaigns", "report"))
    ap.add_argument("--workers", type=int, default=max(os.cpu_count() - 2, 1))
    a = ap.parse_args()
    {"pool": lambda: stage_pool(a.workers),
     "campaigns": lambda: stage_campaigns(a.workers),
     "report": report}[a.stage]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
