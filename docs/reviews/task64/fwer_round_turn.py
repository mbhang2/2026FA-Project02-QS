"""Task 64 — the family-wise error rate of round_turn_mc + max_of_n, measured.

Pre-registration: `docs/reviews/task64/README.md`, committed at 3cd6e4d before
this file ran. C, N, seeds, metric, null, correction and *which numbers get
reported* are fixed there.

Three stages:

    --stage verify   a handful of campaigns, to show the stream is edgeless
                     and the wiring is right. Minutes.
    --stage run      C campaigns. ~4.2 h at 6 workers.
    --stage report   read the raw records, compute both rates, write fwer.json
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
SCRATCH = pathlib.Path(
    os.environ.get("CLAUDE_JOB_DIR", "/tmp")) / "tmp" / "task64"
RAW = SCRATCH / "campaigns.jsonl"
SRC = SCRATCH / "sources"

# -- the pre-registration, in code ------------------------------------------ #
CAMPAIGNS = 1_000
N_TRIALS = 50
Q = 0.05
B_NULL = 10_000
B_MAXN = 10_000
NULL_SEED = 17
MAXN_SEED = 17
SOURCE_SEED = 64_001
DATASET_SEED_BASE = 4_242
WORKERS = 8


def _one_campaign(args):
    """One campaign: N candidates on one dataset, scored and corrected.

    Top level so it can cross a process pool. Everything it calls is the code
    `src/core/loop/campaign.py` calls -- a private re-implementation would
    measure a gate nobody runs.
    """
    campaign_index, paths = args
    sys.path.insert(0, str(REPO))
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.engine import Engine
    from src.core.engine.remote import RemoteStrategy
    from src.core.runlog import RunTicket
    from src.core.scoring import correction
    from src.core.scoring.metrics import IDLE_CHARGE
    from src.core.scoring.randomization import round_turn
    from src.core.scoring.scorer import score
    from src.core.types import Split

    spec = SyntheticSpec(n_sessions=175, ticks_per_session=390,
                         vol_per_tick=0.15,
                         seed=DATASET_SEED_BASE + campaign_index,
                         signal_strength=0.0)
    ticket = RunTicket.for_null_calibration(
        f"task 64 FWER, campaign {campaign_index}")

    observed: list[float] = []
    nulls: list[list[float]] = []
    unscorable = 0
    for path in paths:
        ds = SyntheticDataset(spec)
        try:
            with RemoteStrategy(pathlib.Path(path), "Strategy") as strategy:
                engine = Engine(ds, strategy, ticket=ticket, split=Split.TRAIN,
                                warmup_sessions=0, fill_model="unfavorable",
                                seed=1)
                engine.run()
            blotter = engine.reconstructed()
            calendar = engine.session_calendar()
            s = score(blotter, period=calendar, idle_charge=IDLE_CHARGE)
        except Exception:                                     # noqa: BLE001
            # A crashed candidate is a real trial in this project and is counted
            # as one: it took a look and produced no score.
            unscorable += 1
            continue
        if s.rejected:
            unscorable += 1
            continue
        observed.append(float(s.primary))
        nulls.append(round_turn.null_sample(
            blotter, calendar, b=B_NULL, seed=NULL_SEED,
            idle_charge=IDLE_CHARGE))

    if not observed:
        return {"campaign": campaign_index, "scored": 0,
                "unscorable": unscorable, "best": None,
                "rejected": False, "accepted": False}

    best = max(observed)
    verdict = correction.max_of_n(nulls, best, b=B_MAXN, seed=MAXN_SEED, q=Q)
    rejected = bool(verdict["reject"])
    return {"campaign": campaign_index,
            "scored": len(observed),
            "unscorable": unscorable,
            "best": best,
            "p_value": float(verdict["p"]),
            "rejected": rejected,
            # The gate, per section 3 of the README: rejecting is not accepting.
            "accepted": bool(rejected and best > 0)}


def sources(n: int) -> list[str]:
    """One continuous NullGenerator stream, so campaign k always gets the same
    candidates whether or not campaign k-1 was run in this process."""
    sys.path.insert(0, str(REPO))
    from src.core.loop.agents import NullGenerator

    SRC.mkdir(parents=True, exist_ok=True)
    agent = NullGenerator(seed=SOURCE_SEED)
    out = []
    for i in range(n):
        f = SRC / f"c{i}.py"
        if not f.exists():
            f.write_text(agent.propose([]))
        else:
            agent.propose([])            # keep the stream in step
        out.append(str(f))
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def fingerprint() -> str:
    sys.path.insert(0, str(REPO))
    from src.core.scoring.integrity import code_fingerprint
    return code_fingerprint()


def batches(lo: int, hi: int):
    paths = sources(hi * N_TRIALS)
    for c in range(lo, hi):
        yield (c, paths[c * N_TRIALS:(c + 1) * N_TRIALS])


def stage_verify() -> None:
    """A few campaigns, to show the stream is edgeless and the wiring is right.

    Prints the mean observed score rather than asserting a threshold: an
    assertion here would be a free parameter, and the number is what a reader
    needs to judge whether the generator is doing its job.
    """
    print(f"code_fingerprint {fingerprint()[:16]}")
    t0 = time.monotonic()
    with mp.Pool(WORKERS) as pool:
        rows = pool.map(_one_campaign, list(batches(0, 4)))
    every = [r for r in rows if r["best"] is not None]
    scores = [r["best"] for r in every]
    print(f"{len(rows)} campaigns in {time.monotonic()-t0:.1f}s")
    print(f"  scored/campaign   {sum(r['scored'] for r in rows)/len(rows):.1f}"
          f" of {N_TRIALS}")
    print(f"  unscorable        {sum(r['unscorable'] for r in rows)}")
    print(f"  best score        mean {sum(scores)/len(scores):+.4f}"
          f"  min {min(scores):+.4f}  max {max(scores):+.4f}")
    print(f"  rejected          {sum(r['rejected'] for r in rows)}/{len(rows)}")
    print(f"  ACCEPTED          {sum(r['accepted'] for r in rows)}/{len(rows)}")
    print("\nIf 'best score' is far above zero the stream is not edgeless and "
          "the measurement is about something else.")


def stage_run() -> None:
    fp_before = fingerprint()
    done = set()
    if RAW.exists():
        done = {json.loads(x)["campaign"] for x in RAW.read_text().splitlines()
                if x.strip()}
        print(f"resuming: {len(done)} campaigns already on disk")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    todo = [b for b in batches(0, CAMPAIGNS) if b[0] not in done]
    print(f"code_fingerprint {fp_before[:16]}")
    print(f"{len(todo)} campaigns x {N_TRIALS} trials at {WORKERS} workers")

    t0 = time.monotonic()
    with mp.Pool(WORKERS) as pool, RAW.open("a") as fh:
        for i, row in enumerate(pool.imap_unordered(_one_campaign, todo), 1):
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            if i % 25 == 0:
                rate = i / (time.monotonic() - t0)
                left = (len(todo) - i) / rate / 3600
                print(f"  {i}/{len(todo)}  {rate*3600:.0f}/h  ~{left:.1f}h left",
                      flush=True)
    fp_after = fingerprint()
    if fp_after != fp_before:
        print(f"\nREFUSING TO REPORT: code_fingerprint moved during the run\n"
              f"  before {fp_before}\n  after  {fp_after}", file=sys.stderr)
        sys.exit(1)
    print(f"done in {(time.monotonic()-t0)/3600:.2f}h")


def stage_report() -> None:
    rows = [json.loads(x) for x in RAW.read_text().splitlines() if x.strip()]
    n = len(rows)
    rejects = sum(r["rejected"] for r in rows)
    accepts = sum(r["accepted"] for r in rows)
    bests = sorted(r["best"] for r in rows if r["best"] is not None)
    positives = sum(1 for b in bests if b > 0)

    out = {
        "campaigns": n,
        "trials_per_campaign": N_TRIALS,
        "q": Q,
        "null_source": "round_turn_mc",
        "correction": "max_of_n",
        "idle_charge": 0.18,
        "reject_rate": rejects / n if n else None,
        "reject_ci": wilson(rejects, n),
        "accept_rate": accepts / n if n else None,
        "accept_ci": wilson(accepts, n),
        "campaigns_with_a_positive_best": positives,
        "best_score_median": bests[len(bests) // 2] if bests else None,
        "best_score_min": bests[0] if bests else None,
        "best_score_max": bests[-1] if bests else None,
        "unscorable_trials": sum(r["unscorable"] for r in rows),
        "code_fingerprint": fingerprint(),
    }
    (HERE / "fwer.json").write_text(json.dumps(out, indent=2) + "\n")

    print(f"campaigns                {n}")
    print(f"reject_rate (correction) {out['reject_rate']:.4f}  "
          f"CI [{out['reject_ci'][0]:.4f}, {out['reject_ci'][1]:.4f}]")
    print(f"accept_rate (THE GATE)   {out['accept_rate']:.4f}  "
          f"CI [{out['accept_ci'][0]:.4f}, {out['accept_ci'][1]:.4f}]")
    print(f"campaigns with best > 0  {positives}/{n}")
    print(f"best score  median {out['best_score_median']:+.4f}"
          f"  min {out['best_score_min']:+.4f}  max {out['best_score_max']:+.4f}")
    print(f"unscorable trials        {out['unscorable_trials']}")
    print(f"\nwrote {HERE / 'fwer.json'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                    choices=("verify", "run", "report"))
    a = ap.parse_args()
    {"verify": stage_verify, "run": stage_run, "report": stage_report}[a.stage]()
