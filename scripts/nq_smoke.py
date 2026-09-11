#!/usr/bin/env python3
"""Plumbing check: run the reference strategy over real NQ sessions.

    python scripts/nq_smoke.py --sessions 5            # a prefix, no split meaning
    python scripts/nq_smoke.py --split validation      # the pre-registered split
    python scripts/nq_smoke.py --split validation --fill-spread --cold

**This is not a result.** It confirms that ticks arrive, orders fill, a blotter
is produced and a ledger row is written. It deliberately computes no score
curve, reports no performance, and tabulates no null: the calibration table is
synthetic and does not transfer, because real sessions are not exchangeable and
a real-data null needs a block bootstrap. That is a later task.

It also reports cold- and warm-cache timings, which is the number that decides
how many trials a night the loop can afford.

With `--split` it serves a pre-registered split from `config/calendars/` and
scores it, against the null measured on this dataset's own training sessions.
The holdout is not reachable from here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_PATH = (
    REPO_ROOT / "src" / "core" / "scoring" / "calibration" / "reference.py"
)
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import inventory as inv  # noqa: E402

import run as runner  # noqa: E402
from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.data.nq import CACHE_ROOT, NQDataset, NQSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.types import Split  # noqa: E402


def report_score(engine, record, ds) -> str:
    """Score the split, and say plainly what the FAR beside it is worth.

    The score is here because the plumbing that produces it is what is being
    checked: a calendar, per-session returns, a metric and a ScoreTuple have
    never met real data. The FAR is here for the same reason and no other.

    The FAR is read from the null measured on **this dataset's own training
    sessions**, with a block bootstrap, and `far_for` refuses a table tagged with
    a different dataset. Task 15 scored this same run against the synthetic table
    and got 0.18%; the honest answer is 32%.
    """
    from src.core.scoring.calibration import null as nullmod
    from src.core.scoring.scorer import score, score_diagnostics

    period = engine.session_calendar()
    result = score(engine.blotter, period=period, notional_base=1.0)
    diag = score_diagnostics(engine.blotter, period, notional_base=1.0)
    engine.record.score = result.as_dict()
    engine.record.diagnostics = diag.as_dict()

    print()
    print(f"metric        {result.metric}")
    print(f"primary       {result.primary:+.4f}   significance {result.significance:+.3f}")
    print(f"sharpe        {result.sharpe:+.4f}   skew {result.skew:+.3f}  "
          f"kurtosis {result.kurtosis:+.3f}")
    print(f"n_sessions    {result.n_sessions}   trades {result.trade_count}   "
          f"net {result.net_pnl:+.2f}")
    if result.rejected:
        print(f"REJECTED      {result.reject_reason}")

    # `dataset=` is the whole guard: without it this reads the synthetic table,
    # which is what Task 15 did. A Sortino of 0.0076 came back at a false
    # acceptance of 0.18% against a 0.1% policy; against the real null it is 32%.
    try:
        far = nullmod.far_for(result.primary, result.n_sessions,
                              metric=result.metric, dataset=ds.name)
        table = nullmod.null_path_for(ds.name).name
        print(f"far_for       {far:.4g}   (null: {table})")
    except Exception as exc:
        far = None
        print(f"far_for       unavailable: {type(exc).__name__}: {exc}")
    return (
        "plumbing check on the pre-registered split, scored against the null "
        "measured on this dataset's own training sessions"
    )


def report_fill_spread(engine, ds, split, warmup, args) -> float:
    """The same run under the optimistic model. On synthetic data at low signal
    the entire apparent edge lived in the optimism; what it does here is not
    known until it is run."""
    twin_ds = NQDataset(ds._s3, os.environ["R2_BUCKET"],
                        NQSpec(cache=not args.cold))
    twin = Engine(twin_ds, ReferenceStrategy(), split=split,
                  fill_model="favorable", warmup_sessions=warmup, seed=1)
    twin.run()
    a = twin.blotter.realized_pnl - twin.blotter.commission
    b = engine.blotter.realized_pnl - engine.blotter.commission
    print()
    print(f"favorable     net {a:+.2f}   fills {twin.n_fills:,}   "
          f"rows {len(twin.blotter.rows):,}")
    print(f"unfavorable   net {b:+.2f}   fills {engine.n_fills:,}   "
          f"rows {len(engine.blotter.rows):,}")
    print(f"spread        {a - b:+.2f}   (favorable minus unfavorable)")
    return a - b


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", type=int, default=5,
                    help="run a prefix of N sessions; no split meaning")
    ap.add_argument("--split", choices=("train", "validation"),
                    help="serve a pre-registered split from config/calendars/ "
                         "and score it. The holdout is not offered here.")
    ap.add_argument("--warmup", type=int, default=1,
                    help="sessions served before the split, not actionable")
    ap.add_argument("--fill-spread", action="store_true",
                    help="run the favorable model too and report the spread")
    ap.add_argument("--cold", action="store_true", help="ignore the local cache")
    ap.add_argument("--ledger", type=Path, default=REPO_ROOT / "runs" / "ledger.jsonl")
    args = ap.parse_args()

    s3 = inv.s3_client(12)
    spec = (
        NQSpec(cache=not args.cold)
        if args.split
        else NQSpec(smoke_sessions=args.sessions, cache=not args.cold)
    )
    ds = NQDataset(s3, os.environ["R2_BUCKET"], spec)
    split = Split(args.split) if args.split else Split.VALIDATION
    warmup = args.warmup if args.split else 0

    if args.split:
        scored = ds.scored_sessions(split, warmup)
        sessions = [dt.date.fromordinal(o) for o in scored]
        print(f"dataset   {ds.name}  digest {ds.digest()}")
        print(f"split     {split}  {len(sessions)} scored sessions  "
              f"{sessions[0]} .. {sessions[-1]}  (+{warmup} warmup)")
    else:
        sessions = ds.sessions()[: args.sessions]
        print(f"dataset   {ds.name}  digest {ds.digest()}")
        print(f"sessions  {len(ds.sessions())} available, running "
              f"{sessions[0]} .. {sessions[-1]}")

    t0 = time.monotonic()
    engine = Engine(ds, ReferenceStrategy(), split=split,
                    fill_model="unfavorable", warmup_sessions=warmup, seed=1)
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

    note = "plumbing check on real data; not scored, not a result"
    if args.split:
        note = report_score(engine, record, ds)
    if args.fill_spread:
        spread = report_fill_spread(engine, ds, split, warmup, args)
        if engine.record.diagnostics is not None:
            # The field existed and nothing ever computed it.
            engine.record.diagnostics["fill_model_spread"] = spread

    row = runner._row(record.as_dict(), "harness", "nq-smoke", args.ledger,
                      purpose="smoke", strategy_path=REFERENCE_PATH)
    row["note"] = note
    runner.append_ledger(args.ledger, row)
    print(f"ledger    {args.ledger}")
    if args.cold:
        print(f"cache     bypassed (--cold); warm cache lives under {CACHE_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
