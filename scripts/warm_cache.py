#!/usr/bin/env python3
"""Populate the session cache in one process, before a parallel campaign.

    python scripts/warm_cache.py --check                 # what is missing, no fetch
    python scripts/warm_cache.py --split train
    python scripts/warm_cache.py --split train --split validation

**Why this exists, and why it is single-process on purpose.** `data/` is
symlinked into every worker tree, so the session cache is the one path a
parallel campaign shares for writing. `NQDataset._write_cache` is atomic as of
2026-10-22, so a race no longer corrupts anything -- but atomicity is not the
whole problem. On a cold cache every worker misses the same session, every
worker fetches it from the bucket, and W-way parallelism buys W times the egress
and W copies of the same decode to write identical bytes. Warming once costs one
copy of each.

So this is not a correctness fix on top of the atomic write; it is the thing
that makes a wide run cheap. Run it once per machine per split, then a campaign
at any W reads and never writes.

**The holdout is refused here.** Warming it means reading it, and §8 spends the
one look through `scripts/holdout.py`, which carries the refusals. A cache
warmer is not the place to acquire holdout data; if the holdout is genuinely
needed on a new machine, that path owns the decision and records it.

Nothing here is scored, nothing is written to any ledger, and `global_n()`
cannot see it: the only artefact is the content-keyed cache.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.data.dataset import sessions_for          # noqa: E402
from src.core.types import Split                        # noqa: E402

WARMABLE = ("train", "validation")


def dataset():
    """Built the way `run.py` builds it, so the cache keys match byte for byte."""
    import os

    import boto3

    from src.core.data.nq import NQDataset, NQSpec

    missing = [v for v in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID",
                           "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
               if not os.environ.get(v)]
    if missing:
        sys.exit("the NQ adapter needs read-only bucket credentials; missing "
                 f"{', '.join(missing)}. See docs/setup/R2_ACCESS.md.")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    return NQDataset(client, os.environ["R2_BUCKET"], NQSpec())


def wanted(ds, split_name: str) -> list:
    """The dates of a split, from the pre-registered calendar.

    Via `sessions_for` rather than the calendar's date range, so embargoed
    sessions are excluded here exactly as they are in a trial.
    """
    split = Split[split_name.upper()]
    warm, live = sessions_for(ds.calendar(), split, 0)
    all_sessions = ds.sessions()
    return [all_sessions[i] for i in list(warm) + list(live)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", action="append", choices=WARMABLE + ("holdout",),
                    help="repeatable; default is train and validation")
    ap.add_argument("--check", action="store_true",
                    help="report what is missing and fetch nothing")
    a = ap.parse_args()

    splits = a.split or list(WARMABLE)
    if "holdout" in splits:
        sys.exit(
            "refusing to warm the holdout. Reading it is the look, and §8 spends "
            "that through scripts/holdout.py, which carries the refusals this "
            "script does not.")

    ds = dataset()
    total_missing = 0
    for name in splits:
        days = wanted(ds, name)
        missing = [d for d in days if not ds._cache_path(d).exists()]
        total_missing += len(missing)
        print(f"{name}: {len(days)} sessions, {len(missing)} not cached", flush=True)
        if a.check or not missing:
            continue
        t0 = time.time()
        for i, d in enumerate(missing, 1):
            ds.load(d)                       # reads, writes the cache, scores nothing
            if i % 10 == 0 or i == len(missing):
                rate = i / max(time.time() - t0, 1e-9)
                eta = (len(missing) - i) / rate if rate else 0
                print(f"  {i}/{len(missing)}  {rate*60:.1f}/min  eta {eta/60:.0f}m",
                      flush=True)
    if a.check:
        print(f"total not cached: {total_missing}")
        return 1 if total_missing else 0
    print("warm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
