#!/usr/bin/env python3
"""Build the empirical null and commit it.

    python scripts/tabulate_null.py                 # writes data/calibration/empirical-null.json
    python scripts/tabulate_null.py --runs 200 --trials 100000

Simulates no-signal runs of the immutable reference strategy, pools their
per-session returns, and bootstraps the sampling distribution of the score at
each session count. The output is committed so thresholds are auditable rather
than regenerated, and so a run does not re-simulate a null to score one trial.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.calibration import null as nullmod  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=120,
                    help="independent no-signal runs feeding the session-return pool")
    ap.add_argument("--sessions", type=int, default=175)
    ap.add_argument("--ticks-per-session", type=int, default=390)
    ap.add_argument("--trials", type=int, default=50_000,
                    help="bootstrap draws per session count")
    ap.add_argument("--seed0", type=int, default=10_000)
    ap.add_argument("--out", type=Path, default=nullmod.NULL_PATH)
    ap.add_argument("--pool-from", type=Path,
                    help="reuse the committed session-return pool instead of "
                         "re-simulating it; the bootstrap is then instant")
    args = ap.parse_args()

    t0 = time.monotonic()
    if args.pool_from:
        pool = json.loads(args.pool_from.read_text())["pool"]
        print(f"reusing {len(pool):,} pooled null session returns from "
              f"{args.pool_from}", flush=True)
    else:
        print(f"simulating {args.runs} no-signal runs ...", flush=True)
        pool = nullmod.null_session_returns(
            runs=args.runs,
            n_sessions=args.sessions,
            ticks_per_session=args.ticks_per_session,
            seed0=args.seed0,
        )
        print(f"  pooled {len(pool):,} null session returns "
              f"in {time.monotonic() - t0:.0f}s", flush=True)

    doc = nullmod.tabulate(pool, trials=args.trials)
    doc["provenance"] = {
        "runs": args.runs,
        "sessions_per_run": args.sessions,
        "ticks_per_session": args.ticks_per_session,
        "seed0": args.seed0,
        "strategy": "src/core/calibration/reference.py::ReferenceStrategy",
        "fill_model": "unfavorable",
        "wall_clock_s": round(time.monotonic() - t0, 1),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out.relative_to(REPO_ROOT)}")

    print()
    for metric in doc["metrics"]:
        for n in (30, 50, 100):
            cell = doc["table"][metric].get(str(n))
            if cell and cell["trials"]:
                q = cell["quantiles"]
                print(f"{metric:<22} N={n:<4} share={cell['share_of_draws']:.3f} "
                      f"far=0.05 -> {q.get('0.05')}  far=0.001 -> {q.get('0.001')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
