#!/usr/bin/env python3
"""Build the profile-indexed null and commit it.

    python scripts/tabulate_profile_null.py --strategies 60 --seeds 40

Runs many edgeless strategies over many independent data realizations, buckets
each run by its own measured behavioural profile, and pools the scores. Every
score in a bucket comes from a strategy with no edge on data with no signal.

Committed so thresholds are auditable rather than regenerated, like every other
calibration artifact, and so a campaign does not re-simulate a null to score one
trial.

**Read the bucket populations in the output.** A bucket's p-value floor is
`1/(n+1)` and a bucket under 200 scores is refused at lookup rather than
answered; the whole point of reporting the table is that a thin bucket is visible
instead of quietly unreliable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.engine.remote import RemoteStrategy  # noqa: E402
from src.core.runlog import RunTicket  # noqa: E402
from src.core.loop.agents import NullGenerator  # noqa: E402
from src.core.scoring import metrics as metricsmod  # noqa: E402
from src.core.scoring.randomization.profile_null import (  # noqa: E402
    DEFAULT_PATH, ProfileNull, bucket_of, profile_axes,
)
from src.core.scoring.scorer import score  # noqa: E402
from src.core.types import Split  # noqa: E402


def write_source(source: str, tag: str) -> Path:
    """The generated strategy, as a file. It is never imported here.

    `RunTicket.for_null_calibration` is the route this uses and it does not
    permit in-process execution: the strategies are generated, not committed, and
    running them in the harness's own address space would reopen findings 2 and 3
    for every one of them.
    """
    tmp = REPO_ROOT / "runs" / "tmp-profile"
    tmp.mkdir(parents=True, exist_ok=True)
    f = tmp / f"{tag}.py"
    f.write_text(source)
    return f


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategies", type=int, default=60)
    ap.add_argument("--seeds", type=int, default=40)
    ap.add_argument("--agent-seed", type=int, default=555)
    ap.add_argument("--out", type=Path, default=DEFAULT_PATH)
    args = ap.parse_args()

    agent = NullGenerator(seed=args.agent_seed)
    paths = [write_source(agent.propose([]), f"p{i}")
             for i in range(args.strategies)]
    ticket = RunTicket.for_null_calibration(
        f"profile-indexed null over {args.strategies} generated strategies")

    t0 = time.monotonic()
    buckets: dict[str, list[float]] = {}
    runs = 0
    for si in range(args.seeds):
        spec = SyntheticSpec(n_sessions=175, ticks_per_session=390,
                             vol_per_tick=0.15, seed=5000 + si)
        for ci, path in enumerate(paths):
            with RemoteStrategy(path, "Strategy") as strategy:
                engine = Engine(SyntheticDataset(spec), strategy, ticket=ticket,
                                split=Split.TRAIN, warmup_sessions=0,
                                fill_model="unfavorable", seed=1)
                engine.run()
            blotter = engine.reconstructed()
            period = engine.session_calendar()
            returns = metricsmod.session_returns(blotter, period)
            s = score(blotter, period=period)
            runs += 1
            if s.rejected:
                continue
            axes = profile_axes(returns)
            buckets.setdefault(bucket_of(axes["active"], axes["downside"]),
                               []).append(s.primary)
        print(f"  seed {si + 1}/{args.seeds}, {runs:,} runs, "
              f"{time.monotonic() - t0:.0f}s", flush=True)

    table = ProfileNull(buckets=buckets, provenance={
        "strategies": args.strategies, "seeds": args.seeds,
        "agent": "src.core.loop.agents.NullGenerator",
        "agent_seed": args.agent_seed,
        "dataset": "synthetic", "split": "train",
        "signal_strength": 0.0,
        "runs": runs,
        "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "wall_clock_s": round(time.monotonic() - t0, 1),
    })
    table.write(args.out)

    print()
    print(f"{'bucket':>8} {'n':>7} {'mean':>8} {'sd':>7} {'q95':>8} "
          f"{'floor':>9}  usable")
    for row in table.summary():
        print(f"{row['bucket']:>8} {row['n']:>7,} {row['mean']:>8.3f} "
              f"{row['sd']:>7.3f} {row['q95']:>8.3f} {row['floor']:>9.5f}  "
              f"{row['usable']}")
    print(f"\nwrote {args.out.relative_to(REPO_ROOT)}  "
          f"({runs:,} runs in {time.monotonic() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
