#!/usr/bin/env python3
"""Amendment 18's acceptance control. Simulated data only.

    python scripts/crypto_a18_control.py --workers 16

The arms, seeds and pass conditions are in Amendment 18 of
`docs/design/pre-registration.md`, committed before this code. If the two disagree,
this file is wrong. It runs once, from a clean commit, and does not sign anything.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import crypto_c3b_control as c3b  # noqa: E402
import crypto_gate_control as gate  # noqa: E402
import prereg  # noqa: E402
from src.core.scoring.randomization import round_turn as rt  # noqa: E402

ALPHA = gate.Q / len(gate.VARIANTS)      # q / N, with N = 5
ARMS = {
    "unprofitable": (8.0, tuple(range(300_001, 301_001))),
    "noise": (0.0, tuple(range(301_001, 301_401))),
    "strong": (25.0, tuple(range(301_401, 301_501))),
}
MAX_UNPROFITABLE_PASSES = 36
MAX_NOISE_UPPER = 0.05
MIN_STRONG_PASSES = 80
DEFAULT_PATH = pathlib.Path.home() / "crypto-controls" / "a18" / "control.json"


def verdict(passes: dict[str, int], counts: dict[str, int]) -> dict:
    for arm, (_, seeds) in ARMS.items():
        if counts.get(arm) != len(seeds):
            raise ValueError(f"arm {arm} is defined on {len(seeds)} campaigns, "
                             f"got {counts.get(arm)}")
    checks = {
        "unprofitable_8bp_at_most_36": passes["unprofitable"] <= MAX_UNPROFITABLE_PASSES,
        "noise_wilson_upper_at_most_0.05":
            gate.wilson(passes["noise"], counts["noise"])[1] <= MAX_NOISE_UPPER,
        "strong_25bp_at_least_80": passes["strong"] >= MIN_STRONG_PASSES,
    }
    return {"checks": checks, "accepted": all(checks.values())}


def _job(args) -> dict:
    arm, edge, seed = args
    row = gate.run_campaign(edge, seed, attribution=rt.ATTRIBUTION_FILL, profit_alpha=ALPHA)
    row["arm"] = arm
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_PATH)
    a = ap.parse_args()

    source = c3b.provenance()
    source["amendment_18_in_force"] = prereg.in_force(18)
    c3b.claim(a.out)
    jobs = [(arm, edge, seed) for arm, (edge, seeds) in ARMS.items() for seed in seeds]
    rows = c3b._map(_job, jobs, a.workers)
    counts = {arm: sum(r["arm"] == arm for r in rows) for arm in ARMS}
    passes = {arm: sum(r["arm"] == arm and r["passed"] for r in rows) for arm in ARMS}
    result = verdict(passes, counts)
    for arm in ARMS:
        low, high = gate.wilson(passes[arm], counts[arm])
        print(f"{arm:13} {passes[arm]:4d} of {counts[arm]:4d} passed  "
              f"Wilson 95% [{low:.4f}, {high:.4f}]")
    for name, ok in result["checks"].items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print("Amendment 18 control ACCEPTED" if result["accepted"]
          else "Amendment 18 control FAILED")
    a.out.write_text(json.dumps({**source, "alpha": ALPHA, "passes": passes, "counts": counts,
                                 **result, "rows": rows}, indent=1) + "\n")
    print(f"wrote {a.out}")
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
