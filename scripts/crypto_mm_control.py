#!/usr/bin/env python3
"""Amendment 24's acceptance control: market makers on a simulated order book.

    python scripts/crypto_mm_control.py --pilot 2
    python scripts/crypto_mm_control.py --stage calibrate --workers 3
    python scripts/crypto_mm_control.py --stage run --workers 3

The market: one tick either side of the mid, a small displayed size at the touch, a trade every
10 seconds, and trades
from aggressors who move the mid their own way after they trade by a Poisson number of ticks.
The mean of that jump is the adverse selection a quoting strategy suffers. Its half-spread is one
tick, so a mean jump of one tick is break-even before costs.

Five strategies quote one lot at the best bid and best ask. A skew setting s stops quoting a side
once the position reaches L = max(1, round(10 * (1 - s))) lots that way, under a 10-lot cap. They
run as native kernels with the registered terms: 0 bp maker, 2 bp taker, 1 ms latency.

A campaign selects its best variant by the score and passes only if Amendment 18's Newey-West
lower bound on its mean session return is above zero at q / 5 (Amendment 24 D5).

The grid, seeds and pass conditions are in Amendment 24. Each stage runs once from a clean
commit. `run` reads the settings from `docs/reviews/crypto-a24/edges.json`, committed after
`calibrate` and before any arm is drawn.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import pathlib
import statistics
import sys

import numpy as np

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import crypto_c3b_control as c3b  # noqa: E402
import crypto_gate_control as gate  # noqa: E402
import crypto_reversal_control as reversal  # noqa: E402
import prereg  # noqa: E402
from src.core.data.okx_book import SessionBook  # noqa: E402
from src.core.data.synthetic import SyntheticDataset  # noqa: E402
from src.core.engine import kernel as K  # noqa: E402
from src.core.engine import native  # noqa: E402
from src.core.runlog import RunTicket  # noqa: E402
from src.core.scoring.metrics import session_calendar  # noqa: E402
from src.core.types import DataCapabilities, Side, Split, Tick  # noqa: E402

PROFILE = "btc-okx-hft-train"
LATENCY_NS = 1_000_000
SKEWS = (0.0, 0.25, 0.5, 0.75, 1.0)
CAP_LOTS = 10
ALPHA = gate.Q / len(SKEWS)
# Mean jump, in ticks. Fine where net returns cross zero: the first calibration's grid of 0.1
# put the noise and unprofitable settings on one point (Amendment 24).
IMPACT_GRID = ((0.0, 0.1, 0.2, 0.3) + tuple(round(0.4 + 0.02 * i, 2) for i in range(21))
               + (1.0, 1.5, 2.0))
TRADE_SPACING_NS = 10_000_000_000      # one trade every 10 s, three times the first control's
N_SESSIONS = 150
CALIBRATION_SEEDS = tuple(range(630_001, 630_101))
UNPROFITABLE_SEEDS = tuple(range(631_001, 632_001))
NOISE_SEEDS = tuple(range(632_001, 632_401))
STRONG_SEEDS = tuple(range(632_401, 632_501))
MIN_STRONG_SHARPE = 0.30
SCALE = 10                      # ticks per price unit: a tick is 0.1
START_TICKS = 1_000_000         # 100,000.0
DEPTH_LOTS = 1_000
EDGES_PATH = REPO / "docs" / "reviews" / "crypto-a24" / "edges.json"
DEFAULT_DIR = pathlib.Path.home() / "crypto-controls" / "a24-revised"

KERNEL = """
def kernel(event, book, state, orders):
    if book[B_OK] == 0.0:
        return 0
    position = event[E_POSITION]
    if position > state[4]:
        state[2] = 0.0
    if position < state[4]:
        state[3] = 0.0
    state[4] = float(position)
    working = event[E_N_WORKING]
    if working == 0:
        state[2] = 0.0
        state[3] = 0.0
    bid = book[B_BID_PX]
    ask = book[B_ASK_PX]
    if (state[2] == 1.0 and state[0] != bid) or (state[3] == 1.0 and state[1] != ask):
        count = 0
        for i in range(32):
            if i < working:
                orders[count, O_OP] = OP_CANCEL
                orders[count, O_ORDER_ID] = event[E_WORKING + i]
                count += 1
        state[2] = 0.0
        state[3] = 0.0
        return count
    count = 0
    if state[2] == 0.0 and position < INVENTORY_LIMIT:
        orders[count, O_OP] = OP_BUY
        orders[count, O_KIND] = KIND_LIMIT
        orders[count, O_TIF] = TIF_DAY
        orders[count, O_SIZE] = 1.0
        orders[count, O_LIMIT] = bid
        state[0] = bid
        state[2] = 1.0
        count += 1
    if state[3] == 0.0 and position > -INVENTORY_LIMIT:
        orders[count, O_OP] = OP_SELL
        orders[count, O_KIND] = KIND_LIMIT
        orders[count, O_TIF] = TIF_DAY
        orders[count, O_SIZE] = 1.0
        orders[count, O_LIMIT] = ask
        state[1] = ask
        state[3] = 1.0
        count += 1
    return count
"""


def limit_for(skew: float) -> int:
    return max(1, round(CAP_LOTS * (1 - skew)))


def kernel_source(skew: float) -> str:
    return KERNEL.replace("INVENTORY_LIMIT", str(limit_for(skew)))


class MarketMakingDataset(gate.CryptoControlDataset):
    """Prices on a 0.1 grid whose mid moves after each trade toward the aggressor's side.

    Session timestamps, bounds and calendar come from the parent. The path is drawn session by
    session from the dataset seed, so serving one split gives the same data a full walk would.
    """

    def __init__(self, spec, impact: float):
        self.impact = float(impact)
        self._paths: dict[int, dict] = {}
        super().__init__(spec)

    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(has_trades=True, has_quotes=False, has_book=True)

    def _path(self, session: int) -> dict:
        hit = self._paths.get(session)
        if hit is not None:
            return hit
        start = START_TICKS if session <= 0 else int(self._path(session - 1)["mid_after"][-1])
        ts = np.array([t.ts for t in SyntheticDataset._session_ticks(self, session)], dtype=np.int64)
        n = ts.size
        rng = np.random.default_rng([self.spec.seed, session, 24])
        side = rng.choice(np.array([-1, 1], np.int64), n)
        size = rng.integers(1, 31, n)
        shown = rng.integers(1, 11, (n + 1, 2))
        jump = rng.poisson(self.impact, n)
        noise = rng.integers(-1, 2, n)
        mid_after = start + np.cumsum(side * jump + noise)
        mid_before = np.concatenate(([start], mid_after[:-1]))
        path = {"ts": ts, "side": side.astype(np.int8), "size": size.astype(np.int64),
                "trade_ticks": mid_before + side, "row_ts": np.concatenate(([ts[0] - 1_000_000], ts)),
                "row_mid": np.concatenate(([start], mid_after)), "shown": shown.astype(np.int64),
                "mid_after": mid_after}
        self._paths[session] = path
        return path

    def _session_ticks(self, session: int) -> list:
        p = self._path(int(session))
        return [Tick(int(t), float(k) / SCALE, int(z), int(session), 0, Side.BUY if s > 0 else Side.SELL)
                for t, k, z, s in zip(p["ts"], p["trade_ticks"], p["size"], p["side"])]

    def trades(self, session: int) -> tuple:
        p = self._path(int(session))
        return p["ts"], p["trade_ticks"] / SCALE, p["size"], p["side"]

    def book_for(self, session) -> SessionBook:
        p = self._path(int(session))
        mid = p["row_mid"]
        offsets = np.arange(2, 6, dtype=np.int64)
        depth = {"scale": SCALE,
                 "bid_px": (mid[:, None] - offsets).astype(np.int32),
                 "ask_px": (mid[:, None] + offsets).astype(np.int32),
                 "bid_lots": np.full((mid.size, 4), DEPTH_LOTS, np.int32),
                 "ask_lots": np.full((mid.size, 4), DEPTH_LOTS, np.int32)}
        return SessionBook(p["row_ts"], (mid - 1) / SCALE, (mid + 1) / SCALE, p["shown"][:, 0].copy(),
                           p["shown"][:, 1].copy(), depth=depth)


def spec_for(seed: int):
    base = gate.spec_for(0.0, seed, N_SESSIONS)
    per_day = 86_400_000_000_000 // TRADE_SPACING_NS
    return dataclasses.replace(base, tick_spacing_ns=TRADE_SPACING_NS, ticks_per_session=per_day)


def run_variant(dataset, skew: float, cfg: dict, profile: dict):
    """One market maker through the native engine: its blotter, calendar, terms and fill count."""
    lib = native.load()
    kernel = K.CompiledKernel(kernel_source(skew))
    warmup = int(profile.get("warmup_sessions", 0))
    warm = sorted(dataset.warmup_sessions(Split.TRAIN, warmup))
    scored = list(dataset.scored_sessions(Split.TRAIN, warmup))
    with native.NativeEngine(lib, ticket=RunTicket.for_calibration("crypto market-making control"),
                             latency_ns=LATENCY_NS, require_two_sided=True, book_features=True,
                             book_events=True, inventory_cap=CAP_LOTS) as fast:
        for session in warm + scored:
            fast.run_session(dataset.book_for(session), dataset.trades(session), session,
                             dataset.session_bounds(session)[1], actionable=session not in warm,
                             callback=kernel.callback(lib), user=kernel.user_pointer())
        if kernel.error:
            raise RuntimeError(f"the control kernel stopped with error {kernel.error}")
        blotter = fast.reconstructed(commission_per_contract=profile["commission_per_contract"],
                                     commission_bps=profile["commission_bps"],
                                     maker_commission_bps=profile["maker_commission_bps"])
    calendar = session_calendar(dataset, scored)
    return blotter, calendar, gate.scoring_terms(cfg, profile, dataset, calendar), len(blotter.rows)


def _calibration_job(args) -> dict:
    impact, seed = args
    cfg = gate.load_config()
    profile = cfg["profiles"][PROFILE]
    dataset = MarketMakingDataset(spec_for(seed), impact)
    means, sharpes, fills = [], [], []
    for skew in SKEWS:
        blotter, calendar, terms, count = run_variant(dataset, skew, cfg, profile)
        returns = gate.session_returns(blotter, calendar, **terms)
        mean = math.fsum(returns) / len(returns)
        sd = statistics.stdev(returns)
        means.append(mean)
        sharpes.append(mean / sd if sd > 0 else 0.0)
        fills.append(count)
    return {"impact": impact, "seed": seed, "means": means, "sharpes": sharpes, "fills": fills}


def select_settings(rows: list[dict]) -> dict:
    """Three settings from the calibration, fixed before any arm runs.

    Unprofitable: the smallest impact whose five variants all have a 99% upper bound below zero.
    Strong: the largest impact at which all five average a per-session Sharpe of at least 0.30.
    Noise: among impacts below the unprofitable one, the one whose mean return over the five
    variants and the calibration seeds is closest to zero after costs. They must be ordered
    strong < noise < unprofitable.
    """
    upper, sharpe = {}, {}
    for impact in sorted({r["impact"] for r in rows}):
        sub = [r for r in rows if r["impact"] == impact]
        upper[impact] = [c3b.upper_bound([r["means"][v] for r in sub], reversal.T_CRITICAL_99)
                         for v in range(len(SKEWS))]
        sharpe[impact] = [math.fsum(r["sharpes"][v] for r in sub) / len(sub) for v in range(len(SKEWS))]
    losing = [i for i, u in upper.items() if all(x < 0 for x in u)]
    strong = [i for i, s in sharpe.items() if all(x >= MIN_STRONG_SHARPE for x in s)]
    unprofitable = min(losing) if losing else None
    best = max(strong) if strong else None
    centre = {impact: math.fsum(math.fsum(r["means"]) for r in rows if r["impact"] == impact)
              / (len(SKEWS) * sum(r["impact"] == impact for r in rows)) for impact in upper}
    below = [i for i in centre if unprofitable is not None and i < unprofitable]
    noise = min(below, key=lambda impact: (abs(centre[impact]), impact)) if below else None
    if None in (unprofitable, best) or not best < noise < unprofitable:
        unprofitable = best = noise = None
    return {"unprofitable_impact": unprofitable, "strong_impact": best, "noise_impact": noise,
            "mean_returns": {str(k): v for k, v in centre.items()},
            "upper_bounds": {str(k): v for k, v in upper.items()},
            "mean_sharpes": {str(k): v for k, v in sharpe.items()}}


def run_campaign(impact: float, seed: int) -> dict:
    cfg = gate.load_config()
    profile = cfg["profiles"][PROFILE]
    floor = cfg.get("campaign", {}).get("min_exposed_fraction")
    dataset = MarketMakingDataset(spec_for(seed), impact)
    primaries, bounds, fills, chosen_skews = [], [], [], []
    for skew in SKEWS:
        blotter, calendar, terms, count = run_variant(dataset, skew, cfg, profile)
        fills.append(count)
        result = gate.score(blotter, period=calendar, min_exposed_fraction=floor, **terms)
        if result.rejected:
            continue
        primaries.append(result.primary)
        chosen_skews.append(skew)
        bounds.append(gate.profitability_bound(gate.session_returns(blotter, calendar, **terms), ALPHA))
    if not primaries:
        return {"impact": impact, "seed": seed, "best": None, "passed": False, "fills": fills}
    best = max(primaries)
    chosen = primaries.index(best)
    return {"impact": impact, "seed": seed, "best": best, "skew": chosen_skews[chosen],
            "profitability": bounds[chosen], "fills": fills, "passed": bool(bounds[chosen]["passes"])}


def _job(args) -> dict:
    arm, impact, seed = args
    return {**run_campaign(impact, seed), "arm": arm}


def verdict(passes: dict[str, int], counts: dict[str, int]) -> dict:
    expected = {"unprofitable": len(UNPROFITABLE_SEEDS), "noise": len(NOISE_SEEDS), "strong": len(STRONG_SEEDS)}
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
    jobs = [(impact, seed) for impact in IMPACT_GRID for seed in CALIBRATION_SEEDS]
    rows = c3b._map(_calibration_job, jobs, workers)
    chosen = select_settings(rows)
    path.write_text(json.dumps({**source, **chosen, "rows": rows}) + "\n")
    print(f"unprofitable impact {chosen['unprofitable_impact']}, noise impact {chosen['noise_impact']}, "
          f"strong impact {chosen['strong_impact']}")
    print(f"wrote {path}")
    return 0 if chosen["unprofitable_impact"] is not None and chosen["strong_impact"] is not None else 1


def stage_run(out: pathlib.Path, workers: int) -> int:
    source = c3b.provenance()
    source["amendment_24_in_force"] = prereg.in_force(24)
    settings = json.loads(EDGES_PATH.read_text())
    calibration = json.loads((out / "calibration.json").read_text())
    keys = ("unprofitable_impact", "strong_impact", "noise_impact")
    if any(settings[k] != calibration[k] for k in keys) or None in (settings[k] for k in keys):
        raise SystemExit("the committed settings are not the calibration's")
    path = out / "control.json"
    c3b.claim(path)
    arms = {"unprofitable": (settings["unprofitable_impact"], UNPROFITABLE_SEEDS),
            "noise": (settings["noise_impact"], NOISE_SEEDS), "strong": (settings["strong_impact"], STRONG_SEEDS)}
    jobs = [(arm, impact, seed) for arm, (impact, seeds) in arms.items() for seed in seeds]
    rows = c3b._map(_job, jobs, workers)
    counts = {arm: sum(r["arm"] == arm for r in rows) for arm in arms}
    passes = {arm: sum(r["arm"] == arm and r["passed"] for r in rows) for arm in arms}
    result = verdict(passes, counts)
    for arm in arms:
        low, high = gate.wilson(passes[arm], counts[arm])
        print(f"{arm:13} {passes[arm]:4d} of {counts[arm]:4d}  Wilson 95% [{low:.4f}, {high:.4f}]")
    for name, ok in result["checks"].items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    path.write_text(json.dumps({**source, "settings": settings, "alpha": ALPHA, "passes": passes,
                                "counts": counts, **result, "rows": rows}, indent=1) + "\n")
    print(f"wrote {path}")
    return 0 if result["accepted"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("calibrate", "run"))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--pilot", type=int, default=0, help="seeds per impact from 9301; no verdict")
    ap.add_argument("--out-dir", type=pathlib.Path, default=DEFAULT_DIR)
    a = ap.parse_args()
    if a.pilot:
        jobs = [(impact, 9301 + i) for impact in (0.0, 0.5, 1.0, 1.5) for i in range(a.pilot)]
        for r in c3b._map(_calibration_job, jobs, a.workers):
            print(r["impact"], r["seed"], "fills", r["fills"], "mean", [f"{m:.2e}" for m in r["means"]],
                  "sharpe", [f"{s:+.3f}" for s in r["sharpes"]], flush=True)
        return 0
    if not a.stage:
        ap.error("--stage or --pilot is required")
    return (stage_calibrate if a.stage == "calibrate" else stage_run)(a.out_dir, a.workers)


if __name__ == "__main__":
    raise SystemExit(main())
