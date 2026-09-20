#!/usr/bin/env python3
"""Task 59: the power of `round_turn_mc` + `max_of_n`, against Task 33's curve.

    python docs/reviews/task59/power_round_turn_mc.py --stage runs
    python docs/reviews/task59/power_round_turn_mc.py --stage report

Pre-registered in this directory's README, committed before this file existed.

**Fully paired with Task 33.** Its power sweep seeded candidates as
`34_000 + int(strength * 1000)` per strength, and `NullGenerator` is
deterministic, so these are the identical candidates on the identical data. The
only thing that differs is where each candidate's null comes from. Task 33's
conditional-null curve was 0.067 / 0.533 / 0.967 / 1.000 at strengths
0 / 0.15 / 0.3 / 0.5, crossing 80% at 0.242.

Runs use `RunTicket.for_null_calibration`, so none of them is a trial.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
OUT = pathlib.Path(__file__).parent

N_SESSIONS, TICKS, VOL = 175, 390, 0.15
DATASET_SEED = 4_242                 # Task 33's. Not a parameter.
STRENGTHS = (0.0, 0.15, 0.3, 0.5, 0.8)
CAMPAIGNS, N_TRIALS = 60, 10
B_NULL, Q, WORKERS = 10_000, 0.05, 9

TASK33 = {0.0: 0.0667, 0.15: 0.5333, 0.3: 0.9667, 0.5: 1.0, 0.8: None}


def _run(args) -> dict:
    """One isolated run at a planted strength. Top level for the process pool."""
    path, signal = args
    sys.path.insert(0, str(REPO))
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.engine import Engine
    from src.core.engine.remote import RemoteStrategy
    from src.core.runlog import RunTicket
    from src.core.scoring.randomization.round_turn import round_turns
    from src.core.scoring.scorer import score
    from src.core.types import Split

    ds = SyntheticDataset(SyntheticSpec(
        n_sessions=N_SESSIONS, ticks_per_session=TICKS, vol_per_tick=VOL,
        seed=DATASET_SEED, signal_strength=signal))
    ticket = RunTicket.for_null_calibration("task 59 power for round_turn_mc")
    with RemoteStrategy(pathlib.Path(path), "Strategy") as strategy:
        engine = Engine(ds, strategy, ticket=ticket, split=Split.TRAIN,
                        warmup_sessions=0, fill_model="unfavorable", seed=1)
        engine.run()
    blotter, period = engine.reconstructed(), engine.session_calendar()
    s = score(blotter, period=period)
    turns = round_turns(blotter, period)
    return {"score": None if s.rejected else s.primary,
            "sessions": [c.session for c in period],
            "turns": [[t.gross, t.cost, t.session] for t in turns]}


def sources(n: int, seed: int) -> list[str]:
    """Task 33's stream for this strength. Same seed, same candidates."""
    from src.core.loop.agents import NullGenerator

    gen = NullGenerator(seed=seed)
    return [gen.propose([]) for _ in range(n)]


def stage_runs() -> None:
    tmp = OUT / "_sources"
    tmp.mkdir(parents=True, exist_ok=True)
    for s in STRENGTHS:
        out = OUT / f"runs-s{s}.json"
        if out.exists():
            print(f"strength {s}: already run"); continue
        srcs = sources(CAMPAIGNS * N_TRIALS, seed=34_000 + int(s * 1000))
        paths = []
        for i, src in enumerate(srcs):
            f = tmp / f"pw{s}_{i:05d}.py"
            if not f.exists():
                f.write_text(src)
            paths.append((str(f), s))
        t0 = time.monotonic()
        print(f"strength {s}: {len(paths):,} runs", flush=True)
        done = []
        with ProcessPoolExecutor(max_workers=WORKERS) as pool:
            for i, r in enumerate(pool.map(_run, paths, chunksize=8), start=1):
                done.append(r)
                if i % 200 == 0:
                    rate = i / (time.monotonic() - t0)
                    print(f"  {i}/{len(paths)}  {rate:.1f}/s  eta "
                          f"{(len(paths) - i) / rate / 60:.0f}m", flush=True)
        out.write_text(json.dumps(done))
        print(f"strength {s}: {(time.monotonic() - t0) / 60:.0f} min")


def _gate(campaign: list[dict]) -> dict:
    from src.core.scoring.correction import max_of_n
    from src.core.scoring.randomization.round_turn import RoundTurn, null_from_turns

    samples, scores = [], []
    for k, r in enumerate(campaign):
        if r["score"] is None:
            samples.append([]); continue
        scores.append(r["score"])
        turns = [RoundTurn(gross=g, cost=c, session=s, n_fills=0)
                 for g, c, s in r["turns"]]
        samples.append(null_from_turns(turns, r["sessions"], b=B_NULL,
                                       seed=90_000 + k))
    if not scores:
        return {"accept": False, "best": None, "p": 1.0, "null_max": None}
    best = max(scores)
    g = max_of_n(samples, best, b=B_NULL, q=Q)
    return {"accept": bool(g["reject"] and best > 0), "best": best,
            "p": g["p"], "null_max": g["null_max_mean"]}


def wilson(k: int, n: int) -> list[float]:
    if n == 0:
        return [0.0, 0.0]
    p, z = k / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [max(0.0, c - h), min(1.0, c + h)]


def stage_report() -> None:
    import statistics

    rows = []
    for s in STRENGTHS:
        f = OUT / f"runs-s{s}.json"
        if not f.exists():
            print(f"strength {s}: no runs"); continue
        runs = json.loads(f.read_text())
        camps = [runs[i * N_TRIALS:(i + 1) * N_TRIALS] for i in range(CAMPAIGNS)]
        t0 = time.monotonic()
        with ProcessPoolExecutor(max_workers=WORKERS) as pool:
            got = list(pool.map(_gate, camps, chunksize=2))
        acc = sum(1 for g in got if g["accept"])
        bests = [g["best"] for g in got if g["best"] is not None]
        nulls = [g["null_max"] for g in got if g["null_max"] is not None]
        rows.append({
            "strength": s, "campaigns": len(got), "accepts": acc,
            "power": acc / len(got), "ci": wilson(acc, len(got)),
            "median_best": statistics.median(bests) if bests else None,
            "median_null_max": statistics.median(nulls) if nulls else None,
            "median_p": statistics.median([g["p"] for g in got]),
            "task33_conditional": TASK33.get(s),
        })
        r = rows[-1]
        print(f"  s={s:<5} power {r['power']:.3f} [{r['ci'][0]:.2f},{r['ci'][1]:.2f}]"
              f"  (task33 {r['task33_conditional']})  median best "
              f"{r['median_best']:+.4f}  median null-max {r['median_null_max']:+.4f}"
              f"  median p {r['median_p']:.3f}   {time.monotonic()-t0:.0f}s", flush=True)
    (OUT / "power.json").write_text(json.dumps(
        {"pre_registration": "docs/reviews/task59/README.md",
         "campaigns": CAMPAIGNS, "n": N_TRIALS, "b": B_NULL, "q": Q,
         "task33_curve": TASK33, "rows": rows}, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("runs", "report"))
    a = ap.parse_args()
    (stage_runs if a.stage == "runs" else stage_report)()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
