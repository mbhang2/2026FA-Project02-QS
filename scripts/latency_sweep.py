#!/usr/bin/env python3
"""At which latencies is one fixed strategy profitable? Train split only.

    python scripts/latency_sweep.py --profile nq-train --strategy path/to/strategy.py
    python scripts/latency_sweep.py --profile crypto-btc-train --strategy s.py \\
        --grid-ns 0,1000000,10000000

Amendment 19's latency analysis. It runs the same strategy at each latency in a grid
fixed before the sweep and reports where the Amendment 18 profitability bound holds,
with alpha divided across the grid.

**It is a description, not a search.** Every point is a `check` row in the sweep's own
ledger and never a trial. It refuses validation and holdout profiles. A latency read
off a sweep may not become a campaign's registered latency.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import prereg  # noqa: E402
import run as runner  # noqa: E402
from src.core.costs import row_cost_terms  # noqa: E402
from src.core.engine.reconstruct import reconstruct  # noqa: E402
from src.core.scoring import metrics  # noqa: E402
from src.core.scoring.profitability import profitability_bound  # noqa: E402
from src.core.scoring.terms import scoring_terms  # noqa: E402
from src.core.types import Split  # noqa: E402

Q = 0.05
MS = 1_000_000
DEFAULT_GRIDS = {
    # Binance trade times are whole milliseconds.
    "crypto": (0, MS, 2 * MS, 5 * MS, 10 * MS, 25 * MS, 50 * MS, 100 * MS, 250 * MS, 500 * MS,
               1_000 * MS),
    "default": (0, 10_000, 50_000, 100_000, 500_000, MS, 5 * MS, 10 * MS, 50 * MS, 100 * MS,
                500 * MS, 1_000 * MS),
}
DEFAULT_OUT = pathlib.Path.home() / "latency-sweeps"
_BOOTSTRAP = ""        # replaced only by a test, to run a point as if Amendment 19 were signed
_POINT = """
import pathlib, sys
repo, profile, ledger, strategy, latency = sys.argv[1:6]
sys.path[:0] = [repo, repo + "/scripts"]
{bootstrap}
import run
run.STRATEGY_PATH = pathlib.Path(strategy)
raise SystemExit(run.main(["--human", "--quiet", "--purpose", "check", "--profile", profile,
                           "--ledger", ledger], latency_override_ns=int(latency)))
"""


def default_grid(cfg: dict, profile: dict) -> tuple[int, ...]:
    kind = (cfg.get("datasets") or {}).get(profile.get("dataset"), {}).get("kind")
    return DEFAULT_GRIDS["crypto" if kind == "crypto" else "default"]


def parse_grid(text: str) -> tuple[int, ...]:
    try:
        values = [int(x) for x in text.split(",") if x.strip()]
    except ValueError:
        raise SystemExit(f"--grid-ns takes whole nanoseconds, got {text!r}") from None
    return check_grid(values)


def check_grid(values) -> tuple[int, ...]:
    grid = tuple(values)
    if len(grid) < 2 or list(grid) != sorted(set(grid)) or grid[0] < 0:
        raise SystemExit("the grid needs at least two distinct latencies, ascending, >= 0")
    return grid


def summarise(points: list[dict]) -> dict:
    """Where the bound holds. A gap in that set is reported rather than smoothed over."""
    completed = [p for p in points if p["outcome"] == "completed"]
    good = [p["latency_ns"] for p in completed if p["bound"]["passes"]]
    positive = [p["latency_ns"] for p in completed if (p["bound"]["mean"] or 0) > 0]
    grid = [p["latency_ns"] for p in points]
    contiguous = bool(good) and grid[grid.index(good[0]):grid.index(good[-1]) + 1] == good
    return {
        "n_points": len(points), "n_completed": len(completed),
        "profitable_latencies_ns": good,
        "lowest_profitable_ns": good[0] if good else None,
        "highest_profitable_ns": good[-1] if good else None,
        "profitable_range_is_contiguous": contiguous,
        "positive_mean_latencies_ns": positive,
    }


def evaluate(row: dict, cfg: dict, profile: dict, dataset, alpha: float) -> dict:
    """The point's bound, from its fill log, under the profile's own scoring terms."""
    from replay import load_fill_log

    point = {"latency_ns": (row.get("execution") or {}).get("latency_ns", 0),
             "outcome": row.get("outcome"), "fills": row.get("fills"),
             "trades": (row.get("score") or {}).get("trade_count"),
             "sortino": (row.get("score") or {}).get("primary"),
             "blotter_digest": row.get("blotter_digest"),
             "bound": {"passes": False, "mean": None, "lower_bound": None}}
    if row.get("outcome") != "completed":
        return point
    sessions = dataset.scored_sessions(Split.TRAIN, profile.get("warmup_sessions", 0))
    calendar = metrics.session_calendar(dataset, sessions)
    terms = scoring_terms(cfg, profile, dataset, calendar)
    fills = load_fill_log(row)
    if fills is None and row.get("fills"):
        raise SystemExit(f"the run at {point['latency_ns']} ns stored no fill log")
    blotter = reconstruct(fills or (),
                          commission_per_contract=profile.get("commission_per_contract", 0.01),
                          commission_bps=profile.get("commission_bps", 0.0),
                          **row_cost_terms(row))
    point["bound"] = profitability_bound(metrics.session_returns(blotter, calendar, **terms),
                                         alpha)
    return point


def run_point(profile_name: str, strategy: pathlib.Path, latency: int,
              ledger: pathlib.Path) -> dict:
    """One latency, in a fresh process.

    `run.py` fingerprints the loaded scoring modules and invalidates a run if they
    change. This script loads more of them to read the results, so sharing its process
    would make every honest point look tampered with.
    """
    before = len(ledger.read_text().splitlines()) if ledger.exists() else 0
    proc = subprocess.run(
        [sys.executable, "-c", _POINT.format(bootstrap=_BOOTSTRAP), str(REPO), profile_name,
         str(ledger), str(strategy), str(latency)],
        cwd=str(REPO), capture_output=True, text=True)
    lines = ledger.read_text().splitlines() if ledger.exists() else []
    if len(lines) != before + 1:
        raise SystemExit(f"the run at {latency} ns wrote no ledger row:\n{proc.stderr[-1500:]}")
    return json.loads(lines[-1])


def sweep(profile_name: str, strategy: pathlib.Path, grid: tuple[int, ...],
          out_dir: pathlib.Path) -> dict:
    if not prereg.in_force(19):
        raise SystemExit("Amendment 19 is not in force, so no latency can be applied.")
    cfg = runner.load_config()
    profile = cfg["profiles"].get(profile_name)
    if profile is None:
        raise SystemExit(f"unknown profile {profile_name!r}")
    if Split(profile["split"]) is not Split.TRAIN:
        raise SystemExit("a latency sweep runs on a train profile only")
    floor = runner.latency_resolution_ns(profile)
    below = [x for x in grid if 0 < x < floor]
    if below:
        raise SystemExit(f"{below} ns is below this dataset's {floor} ns timestamp resolution")
    if not strategy.is_file():
        raise SystemExit(f"no strategy at {strategy}")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit(f"{out_dir} is not empty; a sweep writes to a new directory")
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = out_dir / "ledger.jsonl"
    alpha = Q / len(grid)
    dataset = runner.build_dataset(cfg, profile["dataset"], profile["seed"])

    points = []
    for latency in grid:
        row = run_point(profile_name, strategy.resolve(), latency, ledger)
        points.append(evaluate(row, cfg, profile, dataset, alpha))
        p = points[-1]
        print(f"{latency:>14,} ns  {p['outcome']:10} trades {p['trades']}  "
              f"mean {p['bound']['mean']}  lower {p['bound']['lower_bound']}  "
              f"profitable {p['bound']['passes']}", flush=True)
    result = {"profile": profile_name, "strategy": str(strategy), "split": "train", "q": Q,
              "alpha_per_point": alpha, "grid_ns": list(grid), "points": points,
              "summary": summarise(points),
              "note": "Descriptive, on the train split. Not a trial or a pass."}
    (out_dir / "sweep.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result["summary"], indent=1))
    print(f"wrote {out_dir / 'sweep.json'}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="a committed train profile")
    ap.add_argument("--strategy", required=True, type=pathlib.Path)
    ap.add_argument("--grid-ns", help="comma-separated, ascending; default depends on the data")
    ap.add_argument("--out", type=pathlib.Path)
    a = ap.parse_args()
    cfg = runner.load_config()
    profile = cfg["profiles"].get(a.profile) or {}
    grid = parse_grid(a.grid_ns) if a.grid_ns else default_grid(cfg, profile)
    out = a.out or DEFAULT_OUT / f"{a.profile}-{a.strategy.stem}"
    sweep(a.profile, a.strategy, grid, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
