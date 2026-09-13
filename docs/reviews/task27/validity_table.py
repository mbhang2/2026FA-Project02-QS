#!/usr/bin/env python3
"""Item 0: where did held-out FAR validity actually land?

    python docs/reviews/task27/validity_table.py

Task 19 targeted anti-conservatism at n=30, far=0.001 and Task 18's pool recovery
superseded the number mid-flight. The committed verification artifact answers one
split direction; second-pass finding 4 is that the other direction is worse.

So both directions, over the full grid, on the committed pool. `verify_held_out`
tabulates on one contiguous half and measures realized false acceptance on the
other; running it forwards and backwards is the whole of finding 4.

Validity is `measured <= promised`, i.e. ratio <= 1. Conservative costs power and
stays valid; anti-conservative makes the reported false-discovery rate precise
and wrong, and the FAR column is the input to Benjamini-Hochberg.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from src.core.scoring.calibration import null as nullmod  # noqa: E402

TABLE = REPO / "data" / "calibration" / "empirical-null-nq-trades.json"


def both_directions(pool, block_length: int, trials: int) -> dict:
    """Forward: build on the first half. Reverse: build on the second.

    A contiguous split is the right call -- an interleaved one shares every block
    between the halves and would verify a block bootstrap against its own
    training data. The cost is that the halves are two different periods, so the
    two directions answer different questions and both are worth having.
    """
    out = {}
    forward = nullmod.verify_held_out(pool, block_length=block_length,
                                      trials=trials, check_trials=trials)
    out["forward"] = forward
    reversed_pool = list(np.asarray(pool, dtype=float)[::-1])
    out["reverse"] = nullmod.verify_held_out(reversed_pool,
                                             block_length=block_length,
                                             trials=trials, check_trials=trials)
    return out


def render(rows, ns, fars) -> str:
    by = {(r["n"], r["far"]): r for r in rows}
    head = f"{'n':>5} " + "".join(f"{f:>9}" for f in fars)
    lines = [head, "-" * len(head)]
    for n in ns:
        cells = []
        for f in fars:
            r = by.get((n, f))
            ratio = None if r is None else r.get("ratio")
            cells.append("        -" if ratio is None else f"{ratio:>9.2f}")
        lines.append(f"{n:>5} " + "".join(cells))
    return "\n".join(lines)


def main() -> int:
    doc = json.loads(TABLE.read_text())
    pool = doc["pool"]
    block = doc["block_length"]
    trials = 500_000
    print(f"pool {len(pool)} NQ training sessions   block length {block}   "
          f"{trials:,} trials per cell")
    print("cells are measured / promised. <= 1.00 is valid.\n")

    results = both_directions(pool, block, trials)
    summary = {}
    for direction, res in results.items():
        rows = res["rows"]
        ns = sorted({r["n"] for r in rows})
        fars = sorted({r["far"] for r in rows})
        build = "first" if direction == "forward" else "second"
        check = "second" if direction == "forward" else "first"
        print(f"=== {direction}: build on the {build} {res['build_sessions']} "
              f"sessions, check on the {check} {res['check_sessions']} ===")
        print(render(rows, ns, fars))
        bad = res["anti_conservative_cells"]
        print(f"  worst ratio {res['worst_ratio']:.2f}   "
              f"anti-conservative cells {len(bad)}")
        if bad:
            worst = sorted(bad, key=lambda c: -c["ratio"])[:6]
            for c in worst:
                print(f"    n={c['n']:<4} far={c['far']:<7} ratio {c['ratio']:.2f}")
        print()
        summary[direction] = {
            "worst_ratio": res["worst_ratio"],
            "anti_conservative_cells": bad,
            "build_sessions": res["build_sessions"],
            "check_sessions": res["check_sessions"],
            "rows": rows,
        }

    worst = max(r["worst_ratio"] for r in summary.values())
    clean = all(not r["anti_conservative_cells"] for r in summary.values())
    print(f"worst ratio over both directions: {worst:.2f}")
    print("VALID EVERYWHERE" if clean else
          "ANTI-CONSERVATIVE SOMEWHERE -- the FAR column is not a p-value column "
          "in at least one direction")
    (pathlib.Path(__file__).parent / "validity.json").write_text(
        json.dumps({"block_length": block, "trials": trials,
                    "pool_size": len(pool), "directions": summary},
                   indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
