#!/usr/bin/env python3
"""Does the crypto gate pass a planted edge and fail noise? Simulated data only.

    python scripts/crypto_gate_control.py --workers 10
    python scripts/crypto_gate_control.py --pilot 4       # plumbing and timing

**No real crypto data is opened**, so this spends no trial and is no look at any
split. Prices are simulated at BTC scale. Everything after the prices is the real
path: `Engine`, the `crypto-btc-train` profile's costs from `config/run.yaml`,
`scoring_terms` (Amendment 16), `round_turn.null_sample` and `max_of_n`.

**The strategy is the committed reference**, on 1-hour bars. It is the only code
a calibration ticket may run in-process. One simulated campaign is its five
bracket variants, so the gate is a best-of-5.

**Why the null is read apart from the full gate.** At 10 bp a round trip almost
any noise strategy has a negative Sortino, so "noise failed the gate" can be true
because of fees even with a broken null. Rule (b), `max_of_n` p <= q, is therefore
measured alone as well as with rule (a), Sortino > 0.

Pass criteria, fixed before the first control ran:

  C1  noise, rule (b) alone: Wilson 95% upper bound on the rejection rate <= 0.08
  C2  noise, full gate: Wilson 95% upper bound on the pass rate <= 0.05
  C3  5 bp edge, smaller than the 10 bp round-trip fee: same bound on the full gate
  C4  25 bp edge: full-gate pass rate >= 0.80

Simulated edges say whether the gate works. They say nothing about BTC.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.runlog import RunTicket  # noqa: E402
from src.core.scoring.calibration.reference import ReferenceStrategy  # noqa: E402
from src.core.scoring.correction import max_of_n  # noqa: E402
from src.core.scoring.metrics import FundingEvent  # noqa: E402
from src.core.scoring.randomization import round_turn as rt  # noqa: E402
from src.core.scoring.scorer import score  # noqa: E402
from src.core.scoring.terms import scoring_terms  # noqa: E402
from src.core.types import NS_PER_SECOND, Split  # noqa: E402

PROFILE = "crypto-btc-train"
Q = 0.05
B = rt.B_DEFAULT
IDENTITY_TOLERANCE = 1e-3     # the tolerance `scripts/l2_gate.py` applies
START_PRICE = 100_000.0
FUNDING_RATE = 1e-4           # 1 bp per 8 hours, the typical rate Amendment 16 cites
VARIANTS = ((1.0, 1.0), (1.5, 1.5), (2.0, 2.0), (1.0, 2.0), (2.0, 1.0))
ARMS = {0.0: 400, 5.0: 100, 12.0: 100, 25.0: 100}   # edge in bp per hour -> seeds


class CryptoControlDataset(SyntheticDataset):
    """Simulated prices with the two facts `scoring_terms` asks a crypto dataset for.

    The parent's additive walk is read as a log price, so a price cannot reach zero
    and volatility, the planted edge and the fee all stay proportional to the level.
    """

    def __init__(self, spec: SyntheticSpec):
        self._geometric: dict[int, list] = {}
        super().__init__(spec)

    def _additive(self, session: int) -> list:
        return SyntheticDataset._session_ticks(self, session)

    def _session_open(self, session: int) -> float:
        if session <= 0:
            return self.spec.start_price
        previous = self._additive(session - 1)
        return previous[-1].price if previous else self.spec.start_price

    def _session_ticks(self, session: int) -> list:
        hit = self._geometric.get(session)
        if hit is not None:
            return hit
        s0, grid = self.spec.start_price, self.spec.tick_size
        out = [replace(t, price=round(s0 * math.exp((t.price - s0) / s0) / grid) * grid)
               for t in self._additive(session)]
        self._geometric[session] = out
        return out

    def session_open_prices(self, sessions) -> dict[int, float]:
        return {int(s): self._session_ticks(int(s))[0].price for s in sessions}

    def funding_series(self, sessions) -> list[FundingEvent]:
        out = []
        for s in sessions:
            ticks = self._session_ticks(int(s))
            start, _ = self.session_bounds(int(s))
            for hour in (0, 8, 16):
                ts = start + hour * 3600 * NS_PER_SECOND
                mark = next((t.price for t in reversed(ticks) if t.ts <= ts), ticks[0].price)
                out.append(FundingEvent(ts=ts, rate=FUNDING_RATE, mark=mark))
        return out


def spec_for(edge_bp: float, seed: int, n_sessions: int = 300) -> SyntheticSpec:
    """A UTC-day market at BTC scale: 30 s ticks, about 1.6% daily volatility."""
    return SyntheticSpec(
        n_sessions=n_sessions, ticks_per_session=2880,
        tick_spacing_ns=30 * NS_PER_SECOND, start_price=START_PRICE, tick_size=0.1,
        vol_per_tick=30.0, signal_strength=edge_bp * 1e-4 * START_PRICE,
        signal_interval_ticks=120, seed=seed, train_frac=0.8, validation_frac=0.1,
        session_open_utc="00:00", early_close_rate=0.0, length_jitter=0.0,
    )


def load_config() -> dict:
    return yaml.safe_load((REPO / "config" / "run.yaml").read_text())


def run_campaign(edge_bp: float, seed: int, *, n_sessions: int = 300,
                 b: int = B) -> dict:
    """One simulated campaign: five trials, their nulls, and the gate's two rules."""
    cfg = load_config()
    profile = cfg["profiles"][PROFILE]
    dataset = CryptoControlDataset(spec_for(edge_bp, seed, n_sessions))
    ticket = RunTicket.for_calibration("crypto gate control")
    samples, primaries, trades, gaps = [], [], [], []
    for i, (stop, target) in enumerate(VARIANTS):
        strategy = ReferenceStrategy(interval="1h", stop_atr=stop, target_atr=target,
                                     exit_margin_ns=61 * 60 * NS_PER_SECOND)
        engine = Engine(dataset, strategy, ticket=ticket, split=Split.TRAIN,
                        fill_model=profile["fill_model"],
                        warmup_sessions=profile["warmup_sessions"],
                        commission_per_contract=profile["commission_per_contract"],
                        commission_bps=profile["commission_bps"], seed=seed)
        engine.run()
        calendar = engine.session_calendar()
        terms = scoring_terms(cfg, profile, dataset, calendar)
        result = score(engine.blotter, period=calendar,
                       min_exposed_fraction=cfg.get("campaign", {}).get("min_exposed_fraction"),
                       **terms)
        trades.append(result.trade_count)
        if result.rejected:
            samples.append([])
            continue
        identity = rt.identity_from_blotter(engine.blotter, calendar, **terms)
        # A round turn held across 00:00 UTC has its entry fee in the entry session
        # for the score and in the closing session for the null, so the two can
        # differ. Every gap is recorded. Past its tolerance `l2_gate.py` rescoring
        # uses the no-flip draw as the observed value, so this does the same.
        gap = abs(identity.primary - result.primary)
        gaps.append(gap)
        primaries.append(identity.primary if gap > IDENTITY_TOLERANCE else result.primary)
        samples.append(rt.null_sample(engine.blotter, calendar, b=b, seed=70_000 + i, **terms))
    if not primaries:
        return {"edge_bp": edge_bp, "seed": seed, "best": None, "p": 1.0,
                "rule_b": False, "passed": False, "trades": trades, "identity_gaps": gaps}
    best = max(primaries)
    gate = max_of_n(samples, best, b=b, seed=17, q=Q)
    return {"edge_bp": edge_bp, "seed": seed, "best": best, "p": gate["p"],
            "rule_b": bool(gate["reject"]),
            "passed": bool(gate["reject"]) and best > 0, "trades": trades,
            "identity_gaps": gaps}


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """The 95% Wilson interval for k successes in n."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, mid - half), min(1.0, mid + half)


def summarise(rows: list[dict]) -> dict:
    arms = {}
    for edge in sorted({r["edge_bp"] for r in rows}):
        sub = [r for r in rows if r["edge_bp"] == edge]
        n = len(sub)
        b_k, g_k = sum(r["rule_b"] for r in sub), sum(r["passed"] for r in sub)
        arms[edge] = {"n": n, "rule_b": b_k / n, "rule_b_ci": wilson(b_k, n),
                      "gate": g_k / n, "gate_ci": wilson(g_k, n),
                      "best_positive": sum((r["best"] or -1) > 0 for r in sub) / n}
    return arms


def criteria(arms: dict) -> dict:
    def need(edge):
        return arms.get(edge)
    out = {}
    if need(0.0):
        out["C1 noise, rule (b) upper <= 0.08"] = arms[0.0]["rule_b_ci"][1] <= 0.08
        out["C2 noise, full gate upper <= 0.05"] = arms[0.0]["gate_ci"][1] <= 0.05
    if need(5.0):
        out["C3 edge below fees, full gate upper <= 0.05"] = arms[5.0]["gate_ci"][1] <= 0.05
    if need(25.0):
        out["C4 25 bp edge, full gate >= 0.80"] = arms[25.0]["gate"] >= 0.80
    return out


def _job(args):
    return run_campaign(*args)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--pilot", type=int, default=0,
                    help="run this many seeds per arm from 9001, for plumbing and timing; "
                         "criteria are not evaluated")
    ap.add_argument("--out", type=pathlib.Path,
                    default=pathlib.Path.home() / "crypto-controls" / "gate-control.json")
    a = ap.parse_args()

    if a.pilot:
        jobs = [(e, 9001 + s) for e in ARMS for s in range(a.pilot)]
    else:
        jobs = [(e, s + 1) for e, n in ARMS.items() for s in range(n)]
    t0 = time.monotonic()
    rows = []
    with ProcessPoolExecutor(a.workers, max_tasks_per_child=1) as pool:
        for row in pool.map(_job, jobs):
            rows.append(row)
            if len(rows) % 20 == 0 or len(rows) == len(jobs):
                print(f"{len(rows)}/{len(jobs)} campaigns  {time.monotonic() - t0:.0f}s",
                      flush=True)
    arms = summarise(rows)
    print(f"\n{'edge bp':>8} {'n':>4} {'best>0':>7} {'rule b':>7} {'b 95% CI':>16} "
          f"{'gate':>6} {'gate 95% CI':>16}")
    for edge, x in arms.items():
        print(f"{edge:8.1f} {x['n']:4d} {x['best_positive']:7.3f} {x['rule_b']:7.3f} "
              f"[{x['rule_b_ci'][0]:.3f}, {x['rule_b_ci'][1]:.3f}] {x['gate']:6.3f} "
              f"[{x['gate_ci'][0]:.3f}, {x['gate_ci'][1]:.3f}]")
    gaps = [g for r in rows for g in r["identity_gaps"]]
    print(f"\nscore vs null no-flip draw: {sum(g > 1e-12 for g in gaps)} of {len(gaps)} trials "
          f"differ, {sum(g > IDENTITY_TOLERANCE for g in gaps)} past the "
          f"{IDENTITY_TOLERANCE:g} tolerance and rescored, largest {max(gaps, default=0.0):.2e}")
    verdicts = {} if a.pilot else criteria(arms)
    for name, ok in verdicts.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"pilot": bool(a.pilot), "profile": PROFILE, "q": Q, "b": B,
                                 "arms": {str(k): v for k, v in arms.items()},
                                 "criteria": verdicts, "rows": rows}, indent=1) + "\n")
    print(f"wrote {a.out}")
    return 0 if a.pilot or all(verdicts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
