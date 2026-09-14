#!/usr/bin/env python3
"""Why the FAR column over-fires by 17.5x on a known-null campaign.

    python docs/reviews/task30/why_it_fails.py --seeds 40

Two candidate explanations, and they have very different consequences:

1. **The FAR column is broken.** The table's quantiles do not deliver their rate
   even for the strategy they were built from. That would outrank everything.
2. **The null is one strategy's**, exactly as `metric.md` already records, and
   the loop is the first thing that ever pointed 200 *different* strategies at
   it. The table is right about the reference strategy and wrong about the
   variants, because their per-session return distributions are not the
   reference's.

The test: run the **reference strategy** -- the one the null was tabulated from
-- across the same dataset seeds and measure its realized rate. If that lands at
the promised 0.001 while the variants land at 17.5x, explanation 2 is the whole
story and the single-strategy assumption has a number attached to it for the
first time.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.loop.agents import NullGenerator  # noqa: E402
from src.core.scoring import metrics as metricsmod  # noqa: E402
from src.core.scoring.calibration import null as nullmod  # noqa: E402
from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.scoring.scorer import score  # noqa: E402
from src.core.types import Split  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fdr_calibration import load_strategy  # noqa: E402


def walk(strategy_cls, dataset_seed: int):
    spec = SyntheticSpec(n_sessions=175, ticks_per_session=390,
                         vol_per_tick=0.15, seed=dataset_seed)
    engine = Engine.for_test(SyntheticDataset(spec), strategy_cls(),
                             split=Split.TRAIN, warmup_sessions=0,
                             fill_model="unfavorable", seed=1)
    engine.run()
    blotter = engine.reconstructed()
    period = engine.session_calendar()
    returns = metricsmod.session_returns(blotter, period)
    s = score(blotter, period=period)
    far = 1.0 if s.rejected else nullmod.far_for_score(s, dataset="synthetic")["far"]
    return s, returns, far


def describe(label: str, rows) -> dict:
    fars = [r["far"] for r in rows]
    sds = [r["sd"] for r in rows]
    dds = [r["dd"] for r in rows]
    trades = [r["trades"] for r in rows]
    at001 = sum(1 for f in fars if f <= 0.001)
    out = {
        "label": label, "n": len(rows),
        "at_0.001": at001, "rate": at001 / len(rows),
        "ratio_vs_promised": at001 / len(rows) / 0.001,
        "median_return_sd": statistics.median(sds),
        "median_downside_dev": statistics.median(dds),
        "median_trades": statistics.median(trades),
    }
    print(f"  {label:<34} n={out['n']:<5} far<=0.001 in {at001:<4} "
          f"= {out['rate']:.5f}  ({out['ratio_vs_promised']:>6.2f}x promised)")
    print(f"  {'':34} median per-session return sd {out['median_return_sd']:.4f}"
          f"   downside dev {out['median_downside_dev']:.4f}"
          f"   trades {out['median_trades']:.0f}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=40)
    ap.add_argument("--variants", type=int, default=10)
    args = ap.parse_args()
    t0 = time.monotonic()

    print("the pool the null was built from, for comparison:")
    doc = json.loads(
        (REPO / "data" / "calibration" / "empirical-null.json").read_text())
    pool = doc["pool"]
    print(f"  {len(pool):,} reference-strategy session returns   "
          f"sd {statistics.stdev(pool):.4f}")
    dd_pool = (sum(min(0.0, x) ** 2 for x in pool) / len(pool)) ** 0.5
    print(f"  downside deviation {dd_pool:.4f}")
    print()

    reference, variants = [], []
    for c in range(args.seeds):
        seed = 1000 + c
        s, returns, far = walk(ReferenceStrategy, seed)
        reference.append({
            "far": far, "sd": statistics.stdev(returns) if len(returns) > 1 else 0.0,
            "dd": metricsmod.downside_deviation(returns),
            "trades": s.trade_count, "primary": s.primary,
        })
        agent = NullGenerator(seed=c)
        for v in range(args.variants):
            cls = load_strategy(agent.propose([]), f"why{seed}_{v}")
            s, returns, far = walk(cls, seed)
            variants.append({
                "far": far,
                "sd": statistics.stdev(returns) if len(returns) > 1 else 0.0,
                "dd": metricsmod.downside_deviation(returns),
                "trades": s.trade_count, "primary": s.primary,
            })
        if (c + 1) % 10 == 0:
            print(f"  ... {c + 1}/{args.seeds} seeds, "
                  f"{time.monotonic() - t0:.0f}s", flush=True)

    print()
    print("realized rate at the promised 0.001:")
    a = describe("the REFERENCE strategy", reference)
    b = describe("the null generator's variants", variants)
    print()
    print(f"the table is {'right' if a['ratio_vs_promised'] <= 2 else 'wrong'} "
          f"about the strategy it was built from "
          f"({a['ratio_vs_promised']:.2f}x) and "
          f"{'wrong' if b['ratio_vs_promised'] > 2 else 'right'} about the "
          f"variants ({b['ratio_vs_promised']:.2f}x).")
    (pathlib.Path(__file__).parent / "why-it-fails.json").write_text(
        json.dumps({"reference": a, "variants": b,
                    "pool_sd": statistics.stdev(pool),
                    "pool_downside_dev": dd_pool}, indent=1, sort_keys=True))
    print(f"wall {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
