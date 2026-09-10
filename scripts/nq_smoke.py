#!/usr/bin/env python3
"""Plumbing check: run the reference strategy over a handful of real NQ sessions.

    python scripts/nq_smoke.py --sessions 5
    python scripts/nq_smoke.py --sessions 5 --cold

**This is not a result.** It confirms that ticks arrive, orders fill, a blotter
is produced and a ledger row is written. It deliberately computes no score
curve, reports no performance, and tabulates no null: the calibration table is
synthetic and does not transfer, because real sessions are not exchangeable and
a real-data null needs a block bootstrap. That is a later task.

It also reports cold- and warm-cache timings, which is the number that decides
how many trials a night the loop can afford.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import inventory as inv  # noqa: E402

import run as runner  # noqa: E402
from src.core.calibration import ReferenceStrategy  # noqa: E402
from src.core.datasets.nq import CACHE_ROOT, NQDataset, NQSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.types import Split  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", type=int, default=5)
    ap.add_argument("--cold", action="store_true", help="ignore the local cache")
    ap.add_argument("--ledger", type=Path, default=REPO_ROOT / "runs" / "ledger.jsonl")
    args = ap.parse_args()

    s3 = inv.s3_client(12)
    ds = NQDataset(s3, os.environ["R2_BUCKET"],
                   NQSpec(smoke_sessions=args.sessions, cache=not args.cold))
    sessions = ds.sessions()[: args.sessions]
    print(f"dataset   {ds.name}  digest {ds.digest()}")
    print(f"sessions  {len(ds.sessions())} available, running {sessions[0]} .. {sessions[-1]}")

    t0 = time.monotonic()
    engine = Engine(ds, ReferenceStrategy(), split=Split.VALIDATION,
                    fill_model="unfavorable", warmup_sessions=0, seed=1)
    record = engine.run()
    elapsed = time.monotonic() - t0

    ticks = record.ticks
    print()
    print(f"ticks     {ticks:,}  ({ticks / len(sessions):,.0f} per session)")
    print(f"bars      {record.bars_delivered:,}   fills {record.fills:,}")
    print(f"blotter   {len(engine.blotter.rows):,} rows   digest {record.blotter_digest[:16]}")
    print(f"contracts {sorted({ds.contract_for(d).raw_symbol for d in sessions})}")
    print(f"wall      {elapsed:.1f}s   {ticks / max(elapsed, 1e-9):,.0f} ticks/s")

    full = len(ds.sessions())
    per_session = elapsed / len(sessions)
    print()
    print(f"projection: {per_session:.1f}s per session -> "
          f"{per_session * full / 60:.0f} min for all {full} sessions")

    row = runner._row(record.as_dict(), "smoke", "nq-smoke", args.ledger)
    row["strategy"] = "src/core/calibration/reference.py::ReferenceStrategy"
    row["note"] = "plumbing check on real data; not scored, not a result"
    runner.append_ledger(args.ledger, row)
    print(f"ledger    {args.ledger}")
    if args.cold:
        print(f"cache     bypassed (--cold); warm cache lives under {CACHE_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
