#!/usr/bin/env python3
"""Null and power calibration for the simulator's accept gate, two metrics at once.

The new metric (Sortino on per-session returns) and the placeholder it replaced
(a t-statistic on per-trade P&L) are computed on the **same runs**, so the two
acceptance curves differ only in the metric and not in the data. That contrast is
the evidence that this rig catches metric pathologies and not only gate ones.

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

from src.core.calibration import (  # noqa: E402
    AcceptRule,
    EmpiricalAcceptRule,
    ReferenceStrategy,
    acceptance_rate,
)
from src.core.calibration import null as nullmod  # noqa: E402

STRENGTHS = [0.0, 0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=20, help="independent trials per point")
    ap.add_argument("--t-min", type=float, default=3.0,
                    help="parametric rule: threshold on ratio x sqrt(N). Kept so the "
                         "pre-calibration curve stays reproducible")
    ap.add_argument("--far", type=float,
                    help="empirical rule: target false-acceptance rate, threshold "
                         "read off the tabulated null. This is the calibrated path")
    ap.add_argument("--sessions", type=int, default=175)
    ap.add_argument("--ticks-per-session", type=int, default=390)
    ap.add_argument("--fill-model", default="unfavorable")
    ap.add_argument("--json", type=Path, help="also write the curve here")
    args = ap.parse_args()

    if args.far:
        rule = EmpiricalAcceptRule(far=args.far)
        rule_label = (f"empirical: far <= {args.far:g} from the tabulated null, net > 0")
    else:
        rule = AcceptRule(t_min=args.t_min)
        rule_label = f"parametric: ratio x sqrt(N) >= {args.t_min}, net > 0"
    seeds = list(range(1, args.seeds + 1))
    rows = []
    print(f"accept rule: {rule_label}   fill model: {args.fill_model}")
    print(f"strategy:    {ReferenceStrategy.name} (immutable calibration apparatus)")
    print(f"{args.seeds} seeds x {args.sessions} sessions per point\n")
    print(f"{'signal':>7} | {'accept':>7} {'sortino':>8} {'signif':>8} {'sharpe':>7} "
          f"| {'accept':>7} {'per-trade t':>12} | {'mean net':>10} {'trades':>7} {'expo':>5}")
    print(f"{'':>7} | {'-- sortino per session --':^33} "
          f"| {'-- placeholder --':^21} |")
    for s in STRENGTHS:
        rate, results = acceptance_rate(
            ReferenceStrategy,
            signal_strength=s,
            seeds=seeds,
            rule=rule,
            n_sessions=args.sessions,
            ticks_per_session=args.ticks_per_session,
            fill_model=args.fill_model,
        )
        so = statistics.mean(r.score.primary for r in results)
        sg = statistics.mean(r.score.significance for r in results)
        sh = statistics.mean(r.score.sharpe for r in results)
        mn = statistics.mean(r.score.net_pnl for r in results)
        tc = statistics.mean(r.score.trade_count for r in results)
        ex = statistics.mean(r.diagnostics.sessions_with_exposure for r in results)
        prate = sum(r.accepted_placeholder for r in results) / len(results)
        pt = statistics.mean(r.placeholder_t for r in results)
        label = "  (null)" if s == 0 else ""
        print(f"{s:>7.2f} | {rate:>7.2f} {so:>8.3f} {sg:>8.2f} {sh:>7.3f} "
              f"| {prate:>7.2f} {pt:>12.2f} | {mn:>10.2f} {tc:>7.0f} {ex:>5.0f}{label}")
        rows.append(
            {
                "signal_strength": s,
                "acceptance_rate": rate,
                "mean_sortino": so,
                "mean_significance": sg,
                "mean_sharpe": sh,
                "mean_skew": statistics.mean(r.score.skew for r in results),
                "mean_kurtosis": statistics.mean(r.score.kurtosis for r in results),
                "mean_net": mn,
                "mean_trades": tc,
                "mean_sessions_with_exposure": ex,
                "n_sessions": results[0].score.n_sessions,
                "metrics_used": sorted({r.score.metric for r in results}),
                "placeholder_acceptance_rate": prate,
                "placeholder_mean_t": pt,
            }
        )

    null = rows[0]["acceptance_rate"]
    print(f"\nfalse-acceptance rate on a known null: {null:.2%} "
          f"(placeholder: {rows[0]['placeholder_acceptance_rate']:.2%})")
    if null > 0.1:
        print("  -- too high; the gate is not doing its job", file=sys.stderr)

    def monotone(key):
        c = [r[key] for r in rows]
        return c == sorted(c)

    for key, name in (("acceptance_rate", "sortino"),
                      ("placeholder_acceptance_rate", "placeholder")):
        ok = monotone(key)
        print(f"acceptance monotone in signal strength ({name}): "
              f"{'yes' if ok else 'NO'}")
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "metric": "sortino_per_session",
                    "strategy": ReferenceStrategy.name,
                    "accept_rule": rule_label,
                    "far": args.far,
                    "t_min": None if args.far else args.t_min,
                    "seeds": args.seeds,
                    "sessions": args.sessions,
                    "ticks_per_session": args.ticks_per_session,
                    "fill_model": args.fill_model,
                    "curve": rows,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
