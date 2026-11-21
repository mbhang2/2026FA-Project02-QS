#!/usr/bin/env python3
"""C3b: does the crypto gate reject a truly unprofitable edge? Simulated data only.

    python scripts/crypto_c3b_control.py --stage truth --workers 16
    python scripts/crypto_c3b_control.py --stage test --workers 16

The specification is `docs/reviews/crypto-c3b/README.md`, committed before this code.
If the two disagree, this file is wrong.

`truth` measures which simulated edges lose money after costs and selects E*. `test`
runs the gate once at E*. **Each stage runs once.** It claims its output file before
starting and refuses a dirty tree, so a crashed or repeated run needs a human to decide
what happened instead of quietly drawing again.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import crypto_gate_control as gate  # noqa: E402
import prereg  # noqa: E402
from src.core.scoring.randomization import round_turn as rt  # noqa: E402

EDGES_BP = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0)
TRUTH_SEEDS = tuple(range(100_001, 100_201))
TEST_SEEDS = tuple(range(200_001, 201_001))
T_CRITICAL_99 = 2.601          # two-sided 99%, 199 degrees of freedom
MAX_PASSES = 36                # of 1,000: the largest count with Wilson upper <= 0.05
DEFAULT_DIR = pathlib.Path.home() / "crypto-controls" / "c3b"


def upper_bound(values: list[float], t: float = T_CRITICAL_99) -> float:
    """The upper end of the two-sided t interval for the mean of `values`."""
    n = len(values)
    mean = math.fsum(values) / n
    variance = math.fsum((x - mean) ** 2 for x in values) / (n - 1)
    return mean + t * math.sqrt(variance / n)


def summarise_truth(rows: list[dict]) -> dict:
    """{edge: [each variant's upper bound]} from one row per edge and seed."""
    out: dict[float, list[float]] = {}
    for edge in sorted({r["edge_bp"] for r in rows}):
        means = [r["means"] for r in rows if r["edge_bp"] == edge]
        out[edge] = [upper_bound([m[v] for m in means]) for v in range(len(gate.VARIANTS))]
    return out


def select_edge(bounds: dict[float, list[float]]) -> float | None:
    """E*: the largest edge at which every variant's upper bound is below zero."""
    losing = [edge for edge, uppers in bounds.items() if all(u < 0 for u in uppers)]
    return max(losing) if losing else None


def verdict(passes: int, n: int) -> dict:
    if n != len(TEST_SEEDS):
        raise ValueError(f"C3b is defined on {len(TEST_SEEDS)} campaigns, got {n}")
    return {"passes": passes, "n": n, "max_passes": MAX_PASSES,
            "wilson_95": gate.wilson(passes, n), "c3b_passed": passes <= MAX_PASSES}


def claim(path: pathlib.Path) -> None:
    """Take the stage's one run, or refuse."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x") as handle:
            json.dump({"claimed": time.time()}, handle)
    except FileExistsError:
        raise SystemExit(f"{path} exists: this stage has already been claimed. C3b runs "
                         "each stage once. A human must decide what happens next.") from None


def provenance() -> dict:
    """The commit this ran from. Tracked changes would make that commit untrue."""
    def git(*args):
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    if git("diff", "--quiet", "HEAD").returncode != 0:
        raise SystemExit("C3b runs from a clean commit; tracked files have changes.")
    return {"git_sha": git("rev-parse", "HEAD").stdout.strip(),
            "amendment_17_in_force": prereg.in_force(17)}


def _truth_job(args) -> dict:
    edge, seed = args
    return {"edge_bp": edge, "seed": seed, "means": gate.variant_mean_returns(edge, seed)}


def _test_job(args) -> dict:
    edge, seed = args
    return gate.run_campaign(edge, seed, attribution=rt.ATTRIBUTION_FILL)


def _map(job, jobs: list, workers: int) -> list[dict]:
    rows, t0 = [], time.monotonic()
    with ProcessPoolExecutor(workers, max_tasks_per_child=1) as pool:
        for row in pool.map(job, jobs):
            rows.append(row)
            if len(rows) % 50 == 0 or len(rows) == len(jobs):
                print(f"{len(rows)}/{len(jobs)}  {time.monotonic() - t0:.0f}s", flush=True)
    return rows


def stage_truth(out_dir: pathlib.Path, workers: int) -> int:
    source = provenance()
    path = out_dir / "truth.json"
    claim(path)
    rows = _map(_truth_job, [(e, s) for e in EDGES_BP for s in TRUTH_SEEDS], workers)
    bounds = summarise_truth(rows)
    selected = select_edge(bounds)
    for edge, uppers in bounds.items():
        print(f"{edge:4.1f} bp  largest variant upper bound {max(uppers):+.3e}")
    print(f"E* = {selected}" if selected is not None
          else "No edge qualifies: C3b stops and is reported as not run.")
    path.write_text(json.dumps({**source, "selected_edge_bp": selected,
                                "upper_bounds": {str(k): v for k, v in bounds.items()},
                                "rows": rows}, indent=1) + "\n")
    print(f"wrote {path}")
    return 0


def stage_test(out_dir: pathlib.Path, workers: int) -> int:
    source = provenance()
    try:
        truth = json.loads((out_dir / "truth.json").read_text())
        selected = truth["selected_edge_bp"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        raise SystemExit("Step 1 has no finished result; run --stage truth first.") from None
    if selected is None:
        raise SystemExit("Step 1 found no qualifying edge: C3b stops and is reported as not run.")
    path = out_dir / "test.json"
    claim(path)
    rows = _map(_test_job, [(selected, s) for s in TEST_SEEDS], workers)
    result = verdict(sum(r["passed"] for r in rows), len(rows))
    print(f"E* = {selected} bp: {result['passes']} of {result['n']} campaigns passed the gate, "
          f"Wilson 95% [{result['wilson_95'][0]:.4f}, {result['wilson_95'][1]:.4f}]")
    print("C3b PASS" if result["c3b_passed"] else "C3b FAIL")
    path.write_text(json.dumps({**source, "truth_git_sha": truth["git_sha"],
                                "edge_bp": selected, **result, "rows": rows}, indent=1) + "\n")
    print(f"wrote {path}")
    return 0 if result["c3b_passed"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True, choices=("truth", "test"))
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--out-dir", type=pathlib.Path, default=DEFAULT_DIR)
    a = ap.parse_args()
    return (stage_truth if a.stage == "truth" else stage_test)(a.out_dir, a.workers)


if __name__ == "__main__":
    raise SystemExit(main())
