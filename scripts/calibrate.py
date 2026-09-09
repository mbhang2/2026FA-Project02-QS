#!/usr/bin/env python3
"""Null and power calibration for the simulator's accept gate.

    python scripts/calibrate.py                 # default sweep
    python scripts/calibrate.py --seeds 40 --t-min 3.0

Null: pure-noise synthetic data, where the gate should accept essentially
nothing. Power: a planted signal of known strength, swept, giving the curve of
how large an edge has to be before this harness can see it.

Both halves matter. A gate that rejects everything passes the null trivially.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.calibration import AcceptRule, acceptance_rate  # noqa: E402
from src.strategies.current import MomentumStrategy  # noqa: E402

STRENGTHS = [0.0, 0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=20, help="independent trials per point")
    ap.add_argument("--t-min", type=float, default=3.0)
    ap.add_argument("--sessions", type=int, default=60)
    ap.add_argument("--fill-model", default="unfavorable")
    ap.add_argument("--json", type=Path, help="also write the curve here")
    args = ap.parse_args()

    rule = AcceptRule(t_min=args.t_min)
    seeds = list(range(1, args.seeds + 1))
    rows = []
    print(f"accept rule: t >= {args.t_min}, net > 0   fill model: {args.fill_model}")
    print(f"{args.seeds} seeds x {args.sessions} sessions per point\n")
    print(f"{'signal':>7} {'accept':>7} {'mean t':>8} {'mean net':>10} {'trades':>7}")
    for s in STRENGTHS:
        rate, results = acceptance_rate(
            MomentumStrategy,
            signal_strength=s,
            seeds=seeds,
            rule=rule,
            n_sessions=args.sessions,
            fill_model=args.fill_model,
        )
        mt = statistics.mean(r.score.t_stat for r in results)
        mn = statistics.mean(r.score.net_pnl for r in results)
        tc = statistics.mean(r.score.trade_count for r in results)
        label = "  (null)" if s == 0 else ""
        print(f"{s:>7.2f} {rate:>7.2f} {mt:>8.2f} {mn:>10.2f} {tc:>7.0f}{label}")
        rows.append(
            {
                "signal_strength": s,
                "acceptance_rate": rate,
                "mean_t": mt,
                "mean_net": mn,
                "mean_trades": tc,
            }
        )

    null = rows[0]["acceptance_rate"]
    print(f"\nfalse-acceptance rate on a known null: {null:.2%}")
    if null > 0.1:
        print("  -- too high; the gate is not doing its job", file=sys.stderr)
    if args.json:
        args.json.write_text(
            json.dumps(
                {"t_min": args.t_min, "seeds": args.seeds, "curve": rows}, indent=2
            )
            + "\n"
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
