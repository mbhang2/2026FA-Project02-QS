#!/usr/bin/env python3
"""Is the cheap null adequate? Measured against the expensive one.

    python docs/reviews/task31/profile_vs_randomization.py --candidates 12 --r 99

Per-candidate rotation is the null this task argues for and it costs R engine
runs per candidate. Profile-indexed lookup costs nothing per candidate once the
table exists. The brief asked for the approximation to be *measured* rather than
assumed adequate or abandoned, so: the same candidates through both, p-value
against p-value.

**Replication and resolvable effect.** With C candidates the correlation between
the two p-value vectors has standard error roughly `1/sqrt(C-3)`; at C = 12 that
is 0.33, so this can tell "strongly related" from "unrelated" and cannot pin the
correlation to better than about +/- 0.3. The per-candidate absolute differences
are reported as a distribution rather than a mean for the same reason -- with
twelve points a mean hides everything.
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
from src.core.scoring.randomization import candidate_null  # noqa: E402
from src.core.scoring.randomization.profile_null import ProfileNull  # noqa: E402
from src.core.scoring.scorer import score  # noqa: E402
from src.core.types import Split  # noqa: E402

sys.path.insert(0, str(REPO / "docs" / "reviews" / "task30"))
from fdr_calibration import load_strategy  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", type=int, default=12)
    ap.add_argument("--r", type=int, default=99)
    args = ap.parse_args()

    table = ProfileNull.read()
    agent = NullGenerator(seed=4242)
    t0 = time.monotonic()
    rows = []
    for c in range(args.candidates):
        cls = load_strategy(agent.propose([]), f"cmp{c}")
        spec = SyntheticSpec(n_sessions=175, ticks_per_session=390,
                             vol_per_tick=0.15, seed=8000 + c)

        engine = Engine.for_test(SyntheticDataset(spec), cls(), split=Split.TRAIN,
                                 warmup_sessions=0, fill_model="unfavorable",
                                 seed=1)
        engine.run()
        blotter = engine.reconstructed()
        period = engine.session_calendar()
        returns = metricsmod.session_returns(blotter, period)
        s = score(blotter, period=period)
        if s.rejected:
            continue
        look = table.lookup(returns, s.primary)
        rand = candidate_null(lambda cls=cls: cls(),
                              lambda spec=spec: SyntheticDataset(spec),
                              r=args.r, split=Split.TRAIN, warmup_sessions=0,
                              seed0=95_000 + 1000 * c)
        rows.append({
            "candidate": c, "observed": s.primary,
            "bucket": look["bucket"], "n_in_bucket": look["n_in_bucket"],
            "p_profile": look["p"] if look["usable"] else None,
            "p_rotation": rand.p, "rotation_floor": rand.p_floor,
            "degenerate": rand.degenerate,
            "profile_preserved": rand.profile_preserved,
        })
        print(f"  {c + 1}/{args.candidates}  bucket {look['bucket']} "
              f"p_profile {str(look['p'] if look['usable'] else 'n/a'):>8} "
              f"p_rotation {rand.p:.4f}   {time.monotonic() - t0:.0f}s",
              flush=True)

    both = [(r["p_profile"], r["p_rotation"]) for r in rows
            if r["p_profile"] is not None and not r["degenerate"]]
    print()
    print(f"{len(rows)} candidates, {len(both)} with both p-values available")
    if len(both) >= 3:
        xs = [a for a, _ in both]
        ys = [b for _, b in both]
        corr = statistics.correlation(xs, ys)
        diffs = sorted(abs(a - b) for a, b in both)
        print(f"  correlation                 {corr:+.3f}   "
              f"(se about {1 / max(len(both) - 3, 1) ** 0.5:.2f} at this C)")
        print(f"  |p_profile - p_rotation|    median {diffs[len(diffs) // 2]:.3f}"
              f"   max {diffs[-1]:.3f}")
        agree = sum(1 for a, b in both
                    if (a <= 0.05) == (b <= 0.05))
        print(f"  same accept/reject at 0.05  {agree}/{len(both)}")
    print(f"  no usable bucket            "
          f"{sum(1 for r in rows if r['p_profile'] is None)}/{len(rows)}")
    print(f"  profile preserved by rotation "
          f"{sum(1 for r in rows if r['profile_preserved'])}/{len(rows)}")

    (pathlib.Path(__file__).parent / "profile-vs-randomization.json").write_text(
        json.dumps({"r": args.r, "rows": rows}, indent=1, sort_keys=True))
    print(f"\nwall {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
