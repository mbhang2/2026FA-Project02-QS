#!/usr/bin/env python3
"""Measurement 4: what isolation would cost a full trial.

    python docs/reviews/task24/project.py

Combines the three measurements. Per-callback costs are the ones the *prototype*
measured -- real snapshot building, real JSON, real strategy work in the child --
rather than the microbenchmark's floor, because the microbenchmark's child does
nothing and the difference between the two is most of the cost.
"""

from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

# From measure_callbacks.py --nq: 87 scored sessions on the pre-registered
# validation split, projected to the 564 assemblable NQ sessions.
NQ = {
    "sessions": 87, "callbacks": 46_432, "history_calls": 23_823,
    "bars_read": 23_823, "wall_s": 44.81,
}
FULL_SESSIONS = 564
SCALE = FULL_SESSIONS / NQ["sessions"]

# From proto_run.py --sessions 400, 6,145 callbacks, overhead over the in-process
# run of the same data. Synthetic, because the boundary cost is per callback and
# does not care where the ticks came from.
PROTO_CALLBACKS = 6_145
SHAPES = {
    "push 1 bar":                      0.34,
    "push 200 bars, resent each time": 3.45,
    "push 200 bars, incremental":      0.39,
    "push 20,000 bars, incremental":   1.11,
}

# From measure_roundtrip.py: the floor, with a child that does nothing.
FLOOR_US = {"json 1 bar": 14.2, "pickle 1 bar": 12.6,
            "json 200 bars": 125.5, "pickle 200 bars": 51.2}


def main() -> int:
    full_callbacks = NQ["callbacks"] * SCALE
    full_pulls = NQ["history_calls"] * SCALE
    baseline = NQ["wall_s"] * SCALE

    print(f"baseline      {NQ['wall_s']:.1f}s over {NQ['sessions']} NQ sessions "
          f"-> {baseline:.0f}s for {FULL_SESSIONS} (x{SCALE:.2f})")
    print(f"callbacks     {NQ['callbacks']:,} -> {full_callbacks:,.0f}")
    print(f"history calls {NQ['history_calls']:,} -> {full_pulls:,.0f}")
    print()
    print("PUSH A WINDOW -- one message per callback, no pulls")
    print(f"{'shape':<34} {'us/callback':>12} {'full trial':>12} {'of baseline':>12}")
    for name, overhead in SHAPES.items():
        per = overhead / PROTO_CALLBACKS * 1e6
        total = per * full_callbacks / 1e6
        print(f"{name:<34} {per:>11.0f}us {total:>11.1f}s "
              f"{total / baseline * 100:>11.1f}%")
    print()
    print("   floor, with a child that does no work (measure_roundtrip.py):")
    for name, us in FLOOR_US.items():
        total = us * full_callbacks / 1e6
        print(f"   {name:<28} {us:>8.1f}us {total:>11.1f}s "
              f"{total / baseline * 100:>11.1f}%")
    print()
    print("SERVE PULLS -- a callback message plus a round trip per ctx.history")
    per = SHAPES["push 1 bar"] / PROTO_CALLBACKS * 1e6
    trips = full_callbacks + full_pulls
    total = per * trips / 1e6
    print(f"   round trips                {trips:>12,.0f}")
    print(f"   at the measured {per:.0f}us         {total:>11.1f}s "
          f"{total / baseline * 100:>11.1f}%   -- the reference strategy, which "
          f"pulls once per callback")
    print()
    print("   and the bound, which is the problem:")
    print("   a strategy may call ctx.history() any number of times per callback,")
    print("   and an agent optimising its own score has no reason to economise.")
    print(f"   ten pulls per callback -> {per * (full_callbacks * 11) / 1e6:.0f}s "
          f"({per * (full_callbacks * 11) / 1e6 / baseline * 100:.0f}% of baseline);")
    print("   the action cap bounds orders per callback and nothing bounds reads.")
    print()
    print("   the same run's naive payload, if a pull returned the whole view:")
    naive = 283_779_576 * SCALE * 119 / 1e9
    print(f"   {naive:,.0f} GB per trial. Not a protocol, a demonstration that the")
    print("   view has to become a window somewhere.")

    (HERE / "projection.json").write_text(json.dumps({
        "baseline_s": baseline, "full_callbacks": full_callbacks,
        "full_pulls": full_pulls, "scale": SCALE,
        "shapes": {k: {"us_per_callback": v / PROTO_CALLBACKS * 1e6,
                       "full_trial_s": v / PROTO_CALLBACKS * full_callbacks,
                       "pct_of_baseline":
                           v / PROTO_CALLBACKS * full_callbacks / baseline * 100}
                   for k, v in SHAPES.items()},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
