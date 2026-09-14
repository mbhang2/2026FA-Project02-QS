#!/usr/bin/env python3
"""Task 32 item 2: FWER under the *conditional* guarantee, both nulls.

    python docs/reviews/task32/conditional_calibration.py --stage pool
    python docs/reviews/task32/conditional_calibration.py --stage profile
    python docs/reviews/task32/conditional_calibration.py --stage rotation
    python docs/reviews/task32/conditional_calibration.py --stage report

**The guarantee being calibrated (item 1's decision).** Conditional on the
observed training data: *given these sessions, the probability that a campaign
produces any false discovery is <= q.* Randomness is over strategy generation and
over the randomization, not over which market happened. So everything here runs
on **one data realization**, and the between-realization variance that sank the
profile index in Task 31 (31.5% of score variance) does not arise, because
production also has one realization.

**Sizing, pre-registered before running.** Task 31 needed 200 campaigns to
separate 0.05 from 0.10 and 40 could not; this one is sized deliberately and
differently per arm, because the arms cost three orders of magnitude apart.

    profile arm    C = 400 campaigns x N = 25 distinct candidates = 10,000
                   candidates. se at p=0.05 is sqrt(.05*.95/400) = 0.0109, so the
                   95% half-width is 0.021: RESOLVES 0.05 from 0.09, and does not
                   resolve 0.05 from 0.07.

    rotation arm   C = 40 campaigns x N = 25 = 1,000 candidates, R = 30 rotations
                   each = 31,000 isolated runs. se is 0.0345, half-width 0.068:
                   RESOLVES 0.05 from 0.19 only. Sized by what is affordable, and
                   run as a SIDE-BY-SIDE COMPARISON rather than a second
                   certification -- the profile arm is the one that decides which
                   null operates, and it is the one sized to decide it.

Campaigns use **distinct** candidates rather than resampling a shared pool, so
the campaigns are independent and the binomial se above is the right one. A
shared pool would make the effective error larger than binomial and the interval
a lower bound on uncertainty.

Every run goes through the process boundary. `RunTicket.for_null_calibration`
requires it, and Task 32 item 3 gives the reason it matters here specifically: a
candidate that persisted across rotations could recognise repeated session sets
and sandbag its own null.
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
WORK = REPO / "runs" / "tmp-task32"
DATASET_SEED = 4_242              # the one realization everything conditions on

PROFILE_CAMPAIGNS, PROFILE_N = 400, 25
ROTATION_CAMPAIGNS, ROTATION_N, ROTATION_R = 40, 25, 30
CALIB_POOL = 1_500                # disjoint candidates that build the conditional
                                  # profile buckets; never scored as campaigns


def spec():
    from src.core.data.synthetic import SyntheticSpec
    return SyntheticSpec(n_sessions=175, ticks_per_session=390,
                         vol_per_tick=0.15, seed=DATASET_SEED)


def _run(args):
    """One isolated run. Top level so it can cross a process pool."""
    path, dataset_seed, rot_seed = args
    sys.path.insert(0, str(REPO))
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.engine import Engine
    from src.core.engine.remote import RemoteStrategy
    from src.core.runlog import RunTicket
    from src.core.scoring import metrics as metricsmod
    from src.core.scoring.randomization.rotation import RotatedDataset
    from src.core.scoring.scorer import score
    from src.core.types import Split

    ds = SyntheticDataset(SyntheticSpec(n_sessions=175, ticks_per_session=390,
                                        vol_per_tick=0.15, seed=dataset_seed))
    if rot_seed is not None:
        ds = RotatedDataset(ds, seed=rot_seed)
    ticket = RunTicket.for_null_calibration("task 32 conditional calibration")
    with RemoteStrategy(pathlib.Path(path), "Strategy") as strategy:
        engine = Engine(ds, strategy, ticket=ticket, split=Split.TRAIN,
                        warmup_sessions=0, fill_model="unfavorable", seed=1)
        engine.run()
    blotter = engine.reconstructed()
    period = engine.session_calendar()
    returns = metricsmod.session_returns(blotter, period)
    s = score(blotter, period=period)
    if s.rejected:
        return {"score": None}
    from src.core.scoring.randomization.profile_null import profile_axes
    return {"score": s.primary, "axes": profile_axes(returns)}


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


def parallel(jobs, workers: int, label: str):
    t0 = time.monotonic()
    out = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for i, r in enumerate(pool.map(_run, jobs, chunksize=4), start=1):
            out.append(r)
            if i % 500 == 0:
                rate = i / (time.monotonic() - t0)
                print(f"  {label}: {i:,}/{len(jobs):,}  {rate:.1f}/s  "
                      f"eta {(len(jobs) - i) / rate / 60:.0f} min", flush=True)
    return out


def stage_pool(workers: int) -> None:
    """The disjoint calibration pool: conditional profile buckets."""
    paths = sources(CALIB_POOL, seed=32_001, prefix="cal")
    jobs = [(p, DATASET_SEED, None) for p in paths]
    print(f"calibration pool: {len(jobs):,} isolated runs on seed {DATASET_SEED}")
    rows = parallel(jobs, workers, "pool")
    (HERE / "pool.json").write_text(json.dumps(rows))
    got = sum(1 for r in rows if r["score"] is not None)
    print(f"  {got:,} scored, {len(rows) - got:,} rejected")


def stage_profile(workers: int) -> None:
    paths = sources(PROFILE_CAMPAIGNS * PROFILE_N, seed=32_002, prefix="prof")
    jobs = [(p, DATASET_SEED, None) for p in paths]
    print(f"profile arm: {len(jobs):,} isolated runs "
          f"({PROFILE_CAMPAIGNS} campaigns x {PROFILE_N})")
    rows = parallel(jobs, workers, "profile")
    (HERE / "profile-arm.json").write_text(json.dumps(rows))


def stage_rotation(workers: int) -> None:
    paths = sources(ROTATION_CAMPAIGNS * ROTATION_N, seed=32_003, prefix="rot")
    jobs = []
    for ci, p in enumerate(paths):
        jobs.append((p, DATASET_SEED, None))
        for k in range(ROTATION_R):
            jobs.append((p, DATASET_SEED, 700_000 + 1_000 * ci + k))
    print(f"rotation arm: {len(jobs):,} isolated runs "
          f"({ROTATION_CAMPAIGNS} campaigns x {ROTATION_N} x (1 + {ROTATION_R}))")
    rows = parallel(jobs, workers, "rotation")
    (HERE / "rotation-arm.json").write_text(json.dumps(rows))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                    choices=("pool", "profile", "rotation", "report"))
    ap.add_argument("--workers", type=int, default=max(os.cpu_count() - 2, 1))
    args = ap.parse_args()

    if args.stage == "pool":
        stage_pool(args.workers)
    elif args.stage == "profile":
        stage_profile(args.workers)
    elif args.stage == "rotation":
        stage_rotation(args.workers)
    else:
        from report import report          # noqa: F401  (written separately)
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
