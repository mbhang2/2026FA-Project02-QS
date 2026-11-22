#!/usr/bin/env python3
"""Amendment 20's acceptance control: the gate on a simulated order book.

    python scripts/crypto_book_control.py --pilot 2   # plumbing, on separate seeds
    python scripts/crypto_book_control.py --workers 16

The arms, seeds and pass conditions are in Amendment 20. The full run refuses a dirty
tree and runs once, so its criteria are committed before it produces a number.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import crypto_c3b_control as c3b  # noqa: E402
import crypto_gate_control as gate  # noqa: E402
import prereg  # noqa: E402
from src.core.data.okx_book import SessionBook  # noqa: E402
from src.core.engine.fills import BookQueueFill  # noqa: E402
from src.core.scoring.randomization import round_turn as rt  # noqa: E402
from src.core.types import DataCapabilities  # noqa: E402

ALPHA = gate.Q / len(gate.VARIANTS)
LATENCY_NS = 100_000_000
HALF_SPREAD = 0.1
DISPLAYED_LOTS = 1_000
ARMS = {
    "unprofitable": (8.0, tuple(range(400_001, 401_001))),
    "noise": (0.0, tuple(range(401_001, 401_401))),
    "strong": (25.0, tuple(range(401_401, 401_501))),
}
MAX_UNPROFITABLE_PASSES = 36
MAX_NOISE_UPPER = 0.05
MIN_STRONG_PASSES = 80
DEFAULT_PATH = pathlib.Path.home() / "crypto-controls" / "a20" / "control.json"


class BookControlDataset(gate.CryptoControlDataset):
    """The simulated market, with one tick either side of every trade on display."""

    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(has_trades=True, has_quotes=False, has_book=True)

    def book_for(self, session) -> SessionBook:
        ticks = self._session_ticks(int(session))
        px = np.array([t.price for t in ticks], dtype=float)
        lots = np.full(px.size, DISPLAYED_LOTS, dtype=np.int64)
        return SessionBook(np.array([t.ts for t in ticks], dtype=np.int64), px - HALF_SPREAD,
                           px + HALF_SPREAD, lots, lots)


def verdict(passes: dict[str, int], counts: dict[str, int]) -> dict:
    for arm, (_, seeds) in ARMS.items():
        if counts.get(arm) != len(seeds):
            raise ValueError(f"arm {arm} is defined on {len(seeds)} campaigns")
    checks = {
        "unprofitable_8bp_at_most_36": passes["unprofitable"] <= MAX_UNPROFITABLE_PASSES,
        "noise_wilson_upper_at_most_0.05":
            gate.wilson(passes["noise"], counts["noise"])[1] <= MAX_NOISE_UPPER,
        "strong_25bp_at_least_80": passes["strong"] >= MIN_STRONG_PASSES,
    }
    return {"checks": checks, "accepted": all(checks.values())}


def _job(args) -> dict:
    arm, edge, seed = args
    row = gate.run_campaign(edge, seed, attribution=rt.ATTRIBUTION_FILL, profit_alpha=ALPHA,
                            dataset_class=BookControlDataset,
                            engine_kwargs={"fill_model": BookQueueFill.name,
                                           "latency_ns": LATENCY_NS})
    row["arm"] = arm
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--pilot", type=int, default=0, help="seeds per arm from 9101; no verdict")
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_PATH)
    a = ap.parse_args()
    if a.pilot:
        jobs = [(arm, edge, 9101 + i) for arm, (edge, _) in ARMS.items() for i in range(a.pilot)]
        rows = c3b._map(_job, jobs, a.workers)
        for r in rows:
            print(r["arm"], r["seed"], "trades", r["trades"], "rule_b", r["rule_b"],
                  "passed", r["passed"], "max gap", max(r["identity_gaps"]))
        return 0
    source = c3b.provenance()
    source["amendment_20_in_force"] = prereg.in_force(20)
    c3b.claim(a.out)
    jobs = [(arm, edge, seed) for arm, (edge, seeds) in ARMS.items() for seed in seeds]
    rows = c3b._map(_job, jobs, a.workers)
    counts = {arm: sum(r["arm"] == arm for r in rows) for arm in ARMS}
    passes = {arm: sum(r["arm"] == arm and r["passed"] for r in rows) for arm in ARMS}
    result = verdict(passes, counts)
    for arm in ARMS:
        low, high = gate.wilson(passes[arm], counts[arm])
        print(f"{arm:13} {passes[arm]:4d} of {counts[arm]:4d}  Wilson 95% [{low:.4f}, {high:.4f}]")
    for name, ok in result["checks"].items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    a.out.write_text(json.dumps({**source, "alpha": ALPHA, "passes": passes, "counts": counts,
                                 **result, "rows": rows}, indent=1) + "\n")
    print(f"wrote {a.out}")
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
