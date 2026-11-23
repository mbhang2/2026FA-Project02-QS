#!/usr/bin/env python3
"""Amendment 22's acceptance control: strategies that read a simulated order book.

    python scripts/crypto_book_signal_control.py --pilot 2
    python scripts/crypto_book_signal_control.py --stage calibrate --workers 12
    python scripts/crypto_book_signal_control.py --stage run --workers 12

The grid, seeds and pass conditions are in Amendment 22. Each stage runs once from a clean
commit. `run` reads the edges from `docs/reviews/crypto-a22/edges.json`, committed after
`calibrate` and before any arm is drawn.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import sys

import numpy as np

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import crypto_book_control as book  # noqa: E402
import crypto_c3b_control as c3b  # noqa: E402
import crypto_gate_control as gate  # noqa: E402
import crypto_reversal_control as reversal  # noqa: E402
import prereg  # noqa: E402
from src.core.data.okx_book import SessionBook  # noqa: E402
from src.core.engine.fills import BookQueueFill  # noqa: E402
from src.core.scoring.randomization import round_turn as rt  # noqa: E402

PROFILE = "btc-okx-hft-train"
LATENCY_NS = 1_000_000
THRESHOLDS = (0.2, 0.3, 0.4, 0.5, 0.6)
ALPHA = gate.Q / len(THRESHOLDS)
LEAN_ACCURACY = 0.70
EDGE_GRID = tuple(float(x) for x in range(0, 62, 2))
CALIBRATION_SEEDS = tuple(range(600_001, 600_101))
UNPROFITABLE_SEEDS = tuple(range(601_001, 602_001))
NOISE_SEEDS = tuple(range(602_001, 602_401))
STRONG_SEEDS = tuple(range(602_401, 602_501))
MIN_STRONG_SHARPE = 0.30
EDGES_PATH = REPO / "docs" / "reviews" / "crypto-a22" / "edges.json"
DEFAULT_DIR = pathlib.Path.home() / "crypto-controls" / "a22"


class SignalBookDataset(book.BookControlDataset):
    """Displayed size leans toward the next hour's planted drift on 70% of rows."""

    def book_for(self, session) -> SessionBook:
        session = int(session)
        ticks = self._session_ticks(session)
        px = np.array([t.price for t in ticks], dtype=float)
        ts = np.array([t.ts for t in ticks], dtype=np.int64)
        k = self.spec.signal_interval_ticks
        rng = np.random.default_rng([self.spec.seed, session, 22])
        hours = np.arange(px.size) // k
        direction = np.zeros(px.size)
        for hour in np.unique(hours):
            where = np.flatnonzero(hours == hour)
            move = px[where[-1]] - px[where[0]]
            direction[where] = (np.sign(move) if self.spec.signal_strength
                                else rng.choice((-1.0, 1.0)))
        direction[direction == 0] = 1.0
        honest = rng.random(px.size) < LEAN_ACCURACY
        signed = np.where(honest, direction, -direction) * rng.uniform(0.1, 0.9, px.size)
        bid = np.rint(book.DISPLAYED_LOTS * (1 + signed)).astype(np.int64)
        ask = np.rint(book.DISPLAYED_LOTS * (1 - signed)).astype(np.int64)
        # A row 1 ms before each trade holds the previous row's book, so a callback at a
        # trade can read a fresh row strictly before its own millisecond.
        before = np.concatenate(([0], np.arange(px.size - 1)))
        all_ts = np.column_stack((ts - 1_000_000, ts)).ravel()
        order = np.column_stack((before, np.arange(px.size))).ravel()
        return SessionBook(all_ts, px[order] - book.HALF_SPREAD, px[order] + book.HALF_SPREAD,
                           bid[order], ask[order])


class BookImbalanceStrategy:
    """Enter on top-of-book imbalance, leave by market order one hour later."""

    name = "control/book-imbalance"

    def __init__(self, threshold: float):
        self.threshold = threshold

    def setup(self, ctx) -> None:
        ctx.subscribe("1h", emit_empty=True)
        ctx.subscribe("book")

    def on_bars(self, ctx) -> None:
        if ctx.working_order_ids:
            return
        if ctx.position:
            ctx.close()
            return
        snapshot = ctx.book()
        if snapshot is None:
            return
        bid, ask = snapshot.bid_sizes[0], snapshot.ask_sizes[0]
        imbalance = (bid - ask) / (bid + ask)
        if imbalance >= self.threshold:
            ctx.buy(size=1, tag="entry")
        elif imbalance <= -self.threshold:
            ctx.sell(size=1, tag="entry")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass


def run_variant(dataset, threshold: float, cfg: dict, profile: dict, seed: int):
    return gate._run_variant(dataset, 0.0, 0.0, cfg, profile, seed,
                             strategy=BookImbalanceStrategy(threshold),
                             fill_model=BookQueueFill.name, latency_ns=LATENCY_NS,
                             maker_commission_bps=profile["maker_commission_bps"],
                             book_features=True)


def _calibration_job(args) -> dict:
    edge, seed = args
    cfg = gate.load_config()
    profile = cfg["profiles"][PROFILE]
    dataset = SignalBookDataset(gate.spec_for(edge, seed))
    means, sharpes = [], []
    for threshold in THRESHOLDS:
        engine, calendar, terms = run_variant(dataset, threshold, cfg, profile, seed)
        returns = gate.session_returns(engine.blotter, calendar, **terms)
        mean = math.fsum(returns) / len(returns)
        sd = statistics.stdev(returns)
        means.append(mean)
        sharpes.append(mean / sd if sd > 0 else 0.0)
    return {"edge_bp": edge, "seed": seed, "means": means, "sharpes": sharpes}


def select_edges(rows: list[dict]) -> dict:
    upper, sharpe = {}, {}
    for edge in sorted({r["edge_bp"] for r in rows}):
        sub = [r for r in rows if r["edge_bp"] == edge]
        upper[edge] = [c3b.upper_bound([r["means"][v] for r in sub], reversal.T_CRITICAL_99)
                       for v in range(len(THRESHOLDS))]
        sharpe[edge] = [math.fsum(r["sharpes"][v] for r in sub) / len(sub)
                        for v in range(len(THRESHOLDS))]
    losing = [e for e, u in upper.items() if all(x < 0 for x in u)]
    strong = [e for e, s in sharpe.items() if all(x >= MIN_STRONG_SHARPE for x in s)]
    return {"unprofitable_bp": max(losing) if losing else None,
            "strong_bp": min(strong) if strong else None,
            "upper_bounds": {str(k): v for k, v in upper.items()},
            "mean_sharpes": {str(k): v for k, v in sharpe.items()}}


def run_campaign(edge: float, seed: int, *, b: int = gate.B) -> dict:
    cfg = gate.load_config()
    profile = cfg["profiles"][PROFILE]
    floor = cfg.get("campaign", {}).get("min_exposed_fraction")
    dataset = SignalBookDataset(gate.spec_for(edge, seed))
    samples, primaries, bounds, units, gaps = [], [], [], [], []
    for i, threshold in enumerate(THRESHOLDS):
        engine, calendar, terms = run_variant(dataset, threshold, cfg, profile, seed)
        result = gate.score(engine.blotter, period=calendar, min_exposed_fraction=floor, **terms)
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
        samples.append(rt.null_sample(engine.blotter, calendar, b=b, seed=90_000 + i,
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
        "unprofitable_at_most_36": passes["unprofitable"] <= 36,
        "noise_wilson_upper_at_most_0.05": gate.wilson(passes["noise"], counts["noise"])[1] <= 0.05,
        "strong_at_least_80": passes["strong"] >= 80,
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
    source["amendment_22_in_force"] = prereg.in_force(22)
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
    ap.add_argument("--pilot", type=int, default=0, help="seeds per edge from 9301; no verdict")
    ap.add_argument("--out-dir", type=pathlib.Path, default=DEFAULT_DIR)
    a = ap.parse_args()
    if a.pilot:
        jobs = [("pilot", edge, 9301 + i) for edge in (0.0, 30.0) for i in range(a.pilot)]
        for r in c3b._map(_job, jobs, a.workers):
            print(r["edge_bp"], r["seed"], "units", r["risk_units"], "p", r["p"], "passed", r["passed"],
                  "max gap", max(r["identity_gaps"] or [0.0]))
        return 0
    if not a.stage:
        ap.error("--stage or --pilot is required")
    return (stage_calibrate if a.stage == "calibrate" else stage_run)(a.out_dir, a.workers)


if __name__ == "__main__":
    raise SystemExit(main())
