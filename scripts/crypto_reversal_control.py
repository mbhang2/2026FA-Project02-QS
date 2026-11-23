#!/usr/bin/env python3
"""Amendment 21's acceptance control: the gate on strategies that reverse directly.

    python scripts/crypto_reversal_control.py --pilot 2
    python scripts/crypto_reversal_control.py --stage calibrate --workers 12
    python scripts/crypto_reversal_control.py --stage run --workers 12

The grid, seeds and pass conditions are in Amendment 21. Each stage runs once from a clean
commit. `run` reads the edges from `docs/reviews/crypto-a21/edges.json`, committed after
`calibrate` and before any arm is drawn.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import crypto_book_control as book  # noqa: E402
import crypto_c3b_control as c3b  # noqa: E402
import crypto_gate_control as gate  # noqa: E402
import prereg  # noqa: E402
from src.core.engine.fills import BookQueueFill  # noqa: E402
from src.core.scoring.randomization import round_turn as rt  # noqa: E402

HOLD_HOURS = (1, 2, 4, 8, 12)
ALPHA = gate.Q / len(HOLD_HOURS)
EDGE_GRID = tuple(float(x) for x in range(0, 62, 2))
CALIBRATION_SEEDS = tuple(range(500_001, 500_101))
UNPROFITABLE_SEEDS = tuple(range(501_001, 502_001))
NOISE_SEEDS = tuple(range(502_001, 502_401))
STRONG_SEEDS = tuple(range(502_401, 502_501))
T_CRITICAL_99 = 2.626          # two-sided 99%, 99 degrees of freedom
MIN_STRONG_SHARPE = 0.30
MAX_UNPROFITABLE_PASSES = 36
MAX_NOISE_UPPER = 0.05
MIN_STRONG_PASSES = 80
EDGES_PATH = REPO / "docs" / "reviews" / "crypto-a21" / "edges.json"
DEFAULT_DIR = pathlib.Path.home() / "crypto-controls" / "a21"


class ReversalStrategy:
    """Long or short after the first signal, reversing in one order. Never flat by choice."""

    name = "control/direct-reversal"

    def __init__(self, hold_hours: int):
        self.hold_hours = hold_hours
        self._bars = 0

    def setup(self, ctx) -> None:
        ctx.subscribe("1h", emit_empty=True)
        self._bars = 0

    def on_bars(self, ctx) -> None:
        history = ctx.history("1h")
        if not len(history):
            return
        self._bars += 1
        last = history[-1]
        if self._bars % self.hold_hours or ctx.working_order_ids:
            return
        if not last.tick_count or last.close == last.open:
            return
        delta = (1 if last.close > last.open else -1) - ctx.position
        if delta > 0:
            ctx.buy(size=delta, tag="reversal")
        elif delta < 0:
            ctx.sell(size=-delta, tag="reversal")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass


def run_variant(dataset, hold: int, cfg: dict, profile: dict, seed: int):
    return gate._run_variant(dataset, 0.0, 0.0, cfg, profile, seed,
                             strategy=ReversalStrategy(hold), fill_model=BookQueueFill.name,
                             latency_ns=book.LATENCY_NS)


def _calibration_job(args) -> dict:
    edge, seed = args
    cfg = gate.load_config()
    profile = cfg["profiles"][gate.PROFILE]
    dataset = book.BookControlDataset(gate.spec_for(edge, seed))
    means, sharpes = [], []
    for hold in HOLD_HOURS:
        engine, calendar, terms = run_variant(dataset, hold, cfg, profile, seed)
        returns = gate.session_returns(engine.blotter, calendar, **terms)
        mean = math.fsum(returns) / len(returns)
        sd = statistics.stdev(returns)
        means.append(mean)
        sharpes.append(mean / sd if sd > 0 else 0.0)
    return {"edge_bp": edge, "seed": seed, "means": means, "sharpes": sharpes}


def select_edges(rows: list[dict]) -> dict:
    """The largest all-unprofitable edge and the smallest all-strong edge on the grid."""
    upper, sharpe = {}, {}
    for edge in sorted({r["edge_bp"] for r in rows}):
        sub = [r for r in rows if r["edge_bp"] == edge]
        upper[edge] = [c3b.upper_bound([r["means"][v] for r in sub], T_CRITICAL_99)
                       for v in range(len(HOLD_HOURS))]
        sharpe[edge] = [math.fsum(r["sharpes"][v] for r in sub) / len(sub)
                        for v in range(len(HOLD_HOURS))]
    losing = [e for e, u in upper.items() if all(x < 0 for x in u)]
    strong = [e for e, s in sharpe.items() if all(x >= MIN_STRONG_SHARPE for x in s)]
    return {"unprofitable_bp": max(losing) if losing else None,
            "strong_bp": min(strong) if strong else None,
            "upper_bounds": {str(k): v for k, v in upper.items()},
            "mean_sharpes": {str(k): v for k, v in sharpe.items()}}


def run_campaign(edge: float, seed: int, *, b: int = gate.B) -> dict:
    cfg = gate.load_config()
    profile = cfg["profiles"][gate.PROFILE]
    dataset = book.BookControlDataset(gate.spec_for(edge, seed))
    samples, primaries, bounds, units, gaps = [], [], [], [], []
    for i, hold in enumerate(HOLD_HOURS):
        engine, calendar, terms = run_variant(dataset, hold, cfg, profile, seed)
        result = gate.score(engine.blotter, period=calendar,
                            min_exposed_fraction=cfg.get("campaign", {}).get("min_exposed_fraction"),
                            **terms)
        if result.rejected:
            samples.append([])
            continue
        identity = rt.identity_from_blotter(engine.blotter, calendar,
                                            attribution=rt.ATTRIBUTION_FILL,
                                            unit=rt.UNIT_ZERO_CROSSING, **terms)
        rt.require_identity(result.primary, result.metric, identity, rt.ATTRIBUTION_FILL)
        gaps.append(abs(identity.primary - result.primary))
        primaries.append(result.primary)
        units.append(rt.risk_units(engine.blotter, calendar))
        bounds.append(gate.profitability_bound(
            gate.session_returns(engine.blotter, calendar, **terms), ALPHA))
        samples.append(rt.null_sample(engine.blotter, calendar, b=b, seed=80_000 + i,
                                      attribution=rt.ATTRIBUTION_FILL,
                                      unit=rt.UNIT_ZERO_CROSSING, **terms))
    if not primaries:
        return {"edge_bp": edge, "seed": seed, "best": None, "p": 1.0, "rule_b": False,
                "passed": False, "risk_units": units, "identity_gaps": gaps}
    best = max(primaries)
    chosen = primaries.index(best)
    verdict_b = gate.max_of_n(samples, best, b=b, seed=17, q=gate.Q)
    enough = units[chosen] >= rt.MIN_RISK_UNITS
    return {"edge_bp": edge, "seed": seed, "best": best, "p": verdict_b["p"],
            "rule_b": bool(verdict_b["reject"]), "profitability": bounds[chosen],
            "risk_units": units, "selected_units": units[chosen], "identity_gaps": gaps,
            "passed": bool(verdict_b["reject"] and bounds[chosen]["passes"] and enough)}


def _job(args) -> dict:
    arm, edge, seed = args
    return {**run_campaign(edge, seed), "arm": arm}


def verdict(passes: dict[str, int], counts: dict[str, int]) -> dict:
    expected = {"unprofitable": len(UNPROFITABLE_SEEDS), "noise": len(NOISE_SEEDS),
                "strong": len(STRONG_SEEDS)}
    if counts != expected:
        raise ValueError(f"the arms are defined on {expected}, got {counts}")
    checks = {
        "unprofitable_at_most_36": passes["unprofitable"] <= MAX_UNPROFITABLE_PASSES,
        "noise_wilson_upper_at_most_0.05":
            gate.wilson(passes["noise"], counts["noise"])[1] <= MAX_NOISE_UPPER,
        "strong_at_least_80": passes["strong"] >= MIN_STRONG_PASSES,
    }
    return {"checks": checks, "accepted": all(checks.values())}


def stage_calibrate(out: pathlib.Path, workers: int) -> int:
    source = c3b.provenance()
    path = out / "calibration.json"
    c3b.claim(path)
    jobs = [(edge, seed) for edge in EDGE_GRID for seed in CALIBRATION_SEEDS]
    rows = c3b._map(_calibration_job, jobs, workers)
    chosen = select_edges(rows)
    path.write_text(json.dumps({**source, **chosen, "rows": rows}) + "\n")
    print(f"unprofitable edge {chosen['unprofitable_bp']} bp, strong edge {chosen['strong_bp']} bp")
    print(f"wrote {path}")
    return 0 if chosen["unprofitable_bp"] is not None and chosen["strong_bp"] is not None else 1


def stage_run(out: pathlib.Path, workers: int) -> int:
    source = c3b.provenance()
    source["amendment_21_in_force"] = prereg.in_force(21)
    edges = json.loads(EDGES_PATH.read_text())
    calibration = json.loads((out / "calibration.json").read_text())
    if (edges["unprofitable_bp"], edges["strong_bp"]) != (calibration["unprofitable_bp"],
                                                          calibration["strong_bp"]):
        raise SystemExit("the committed edges are not the calibration's")
    path = out / "control.json"
    c3b.claim(path)
    arms = {"unprofitable": (edges["unprofitable_bp"], UNPROFITABLE_SEEDS),
            "noise": (0.0, NOISE_SEEDS), "strong": (edges["strong_bp"], STRONG_SEEDS)}
    jobs = [(arm, edge, seed) for arm, (edge, seeds) in arms.items() for seed in seeds]
    rows = c3b._map(_job, jobs, workers)
    counts = {arm: sum(r["arm"] == arm for r in rows) for arm in arms}
    passes = {arm: sum(r["arm"] == arm and r["passed"] for r in rows) for arm in arms}
    result = verdict(passes, counts)
    for arm in arms:
        low, high = gate.wilson(passes[arm], counts[arm])
        print(f"{arm:13} {passes[arm]:4d} of {counts[arm]:4d}  Wilson 95% [{low:.4f}, {high:.4f}]")
    for name, ok in result["checks"].items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    path.write_text(json.dumps({**source, "edges": edges, "alpha": ALPHA, "passes": passes,
                                "counts": counts, **result, "rows": rows}, indent=1) + "\n")
    print(f"wrote {path}")
    return 0 if result["accepted"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("calibrate", "run"))
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--pilot", type=int, default=0, help="seeds per edge from 9201; no verdict")
    ap.add_argument("--out-dir", type=pathlib.Path, default=DEFAULT_DIR)
    a = ap.parse_args()
    if a.pilot:
        jobs = [("pilot", edge, 9201 + i) for edge in (0.0, 20.0) for i in range(a.pilot)]
        for r in c3b._map(_job, jobs, a.workers):
            print(r["edge_bp"], r["seed"], "units", r["risk_units"], "p", r["p"], "passed", r["passed"],
                  "max gap", max(r["identity_gaps"] or [0.0]))
        return 0
    if not a.stage:
        ap.error("--stage or --pilot is required")
    return (stage_calibrate if a.stage == "calibrate" else stage_run)(a.out_dir, a.workers)


if __name__ == "__main__":
    raise SystemExit(main())
