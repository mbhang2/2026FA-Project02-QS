#!/usr/bin/env python3
"""The spike's acceptance criterion: is the boundary faithful?

    python docs/reviews/task24/proto_run.py --sessions 1
    python docs/reviews/task24/proto_run.py --sessions 41 --window 4

Runs the reference strategy twice over the same data -- once in process, once
through the subprocess protocol -- and compares blotter digests. A byte-identical
blotter is the only result that says the boundary preserves semantics; anything
short of it is a guess.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from docs.reviews.task24.proto.adapter import SubprocessStrategy  # noqa: E402
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.types import Split  # noqa: E402

REFERENCE = REPO / "src" / "core" / "scoring" / "calibration" / "reference.py"


def dataset(sessions: int) -> SyntheticDataset:
    return SyntheticDataset(SyntheticSpec(
        n_sessions=sessions, ticks_per_session=390, vol_per_tick=0.15, seed=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=40,
                    help="total synthetic sessions; the validation split is 20%%")
    ap.add_argument("--window", type=int, default=1,
                    help="bars the strategy can reach back over")
    ap.add_argument("--sandbox", action="store_true",
                    help="macOS: run the child under docs/reviews/task24/strategy.sb")
    ap.add_argument("--incremental", action="store_true",
                    help="send only bars the child has not seen; it keeps its own "
                         "rolling window of --window size")
    args = ap.parse_args()

    t0 = time.perf_counter()
    inproc = Engine.for_test(dataset(args.sessions), ReferenceStrategy(),
                             split=Split.VALIDATION, warmup_sessions=1,
                             fill_model="unfavorable", seed=1)
    inproc.run()
    in_wall = time.perf_counter() - t0

    t0 = time.perf_counter()
    with SubprocessStrategy(REFERENCE, "ReferenceStrategy",
                            window=args.window,
                            incremental=args.incremental,
                            sandbox=args.sandbox) as remote:
        isolated = Engine.for_test(dataset(args.sessions), remote,
                                   split=Split.VALIDATION, warmup_sessions=1,
                                   fill_model="unfavorable", seed=1)
        isolated.run()
        messages, sent = remote.messages, remote.bytes_out
    out_wall = time.perf_counter() - t0

    same = inproc.blotter.digest() == isolated.blotter.digest()
    scored = len(inproc.record.end_of_session_positions)

    print(f"sessions scored   {scored}   window {args.window} bars"
          f"{'  (incremental)' if args.incremental else ''}")
    print(f"in process        {inproc.blotter.digest()}")
    print(f"                  {len(inproc.blotter.rows):,} rows, "
          f"{inproc.n_fills:,} fills, {in_wall:.2f}s")
    print(f"through a pipe    {isolated.blotter.digest()}")
    print(f"                  {len(isolated.blotter.rows):,} rows, "
          f"{isolated.n_fills:,} fills, {out_wall:.2f}s")
    print(f"fill log          "
          f"{'same' if inproc.fill_log_digest() == isolated.fill_log_digest() else 'DIFFERENT'}"
          f"   {inproc.fill_log_digest()[:16]}")
    print()
    print(f"messages          {messages:,}   {sent / 1e6:.3f} MB sent")
    print(f"                  {sent / max(messages, 1):.0f} bytes per message")
    print(f"overhead          {out_wall - in_wall:+.2f}s  "
          f"({(out_wall / in_wall - 1) * 100:+.0f}% on a run that is almost all "
          f"boundary)")
    print()
    print("BYTE-IDENTICAL" if same else "MISMATCH -- the boundary changed something")
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())
