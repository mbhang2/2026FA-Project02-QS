#!/usr/bin/env python3
"""Populate the crypto session cache before a crypto run, in parallel.

    python scripts/warm_crypto_cache.py --dataset crypto-btc --check
    python scripts/warm_crypto_cache.py --dataset crypto-btc --split train --limit 6   # try it first
    python scripts/warm_crypto_cache.py --dataset crypto-btc --split train --workers 8

The crypto twin of `scripts/warm_cache.py`. A first read of a Binance session
fetches its archive from R2, decodes ~0.3-1.7M trades and writes
`data/cache/crypto-sessions/`: ~10 s a session, so a cold 974-session train split
is close to three hours inside the first trial and would dominate any timing of
it. Warming once beforehand makes every trial a warm read.

**Parallel, unlike `warm_cache.py`, and safe for the reason that one gives.** The
cache is content-keyed on each file's SHA-256 and written atomically, so workers
that met on one session would write identical bytes; here they never meet,
because each session is given to one worker.

**The holdout is refused**, as in `warm_cache.py`: warming it means reading it,
and that look belongs to `scripts/holdout.py`. Nothing is scored and no ledger
row is written.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WARMABLE = ("train", "validation")


def _dataset(name: str):
    import run

    return run.build_dataset(run.load_config(), name, 1)


def _wanted(ds, split_name: str) -> list:
    """The sessions a run on this split reads: warmup and actionable, as a trial does."""
    from src.core.data.dataset import sessions_for
    from src.core.types import Split

    warm, live = sessions_for(ds.calendar(), Split[split_name.upper()], 1)
    every = ds.sessions()
    return [every[i] for i in list(warm) + list(live)]


def _load(name: str, days: list) -> int:
    """One worker: build the dataset once (an S3 client does not cross processes)."""
    ds = _dataset(name)
    for d in days:
        ds.load(d)
    return len(days)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="crypto-btc",
                    help="a crypto dataset in config/run.yaml (crypto-btc, crypto-eth)")
    ap.add_argument("--split", action="append", choices=WARMABLE + ("holdout",),
                    help="repeatable; default is train and validation")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--check", action="store_true",
                    help="report what is missing and fetch nothing")
    ap.add_argument("--limit", type=int, default=None,
                    help="warm only the first N missing sessions of each split (a trial run)")
    a = ap.parse_args()

    splits = a.split or list(WARMABLE)
    if "holdout" in splits:
        sys.exit("refusing to warm the holdout. Reading it is the look, and §8 spends "
                 "that through scripts/holdout.py, which carries the refusals this "
                 "script does not.")
    ds = _dataset(a.dataset)
    if not hasattr(ds, "_cache_path") or not ds.name.startswith("crypto-trades:"):
        sys.exit(f"{a.dataset} is not a crypto dataset; NQ is warmed by scripts/warm_cache.py")

    total_missing = 0
    for name in splits:
        days = _wanted(ds, name)
        missing = [d for d in days if not ds._cache_path(d).exists()]
        total_missing += len(missing)
        print(f"{a.dataset} {name}: {len(days)} sessions, {len(missing)} not cached", flush=True)
        if a.limit is not None and not a.check:
            missing = missing[: a.limit]
        if a.check or not missing:
            continue
        # Small batches, so progress is visible and a failure costs little.
        batches = [missing[i:i + 5] for i in range(0, len(missing), 5)]
        t0, done = time.time(), 0
        with ProcessPoolExecutor(a.workers) as pool:
            futs = [pool.submit(_load, a.dataset, b) for b in batches]
            for fut in as_completed(futs):
                done += fut.result()
                rate = done / max(time.time() - t0, 1e-9)
                print(f"  {done}/{len(missing)}  {rate * 60:.1f}/min  "
                      f"eta {(len(missing) - done) / rate / 60 if rate else 0:.0f}m", flush=True)
    if a.check:
        print(f"total not cached: {total_missing}")
        return 1 if total_missing else 0
    print("warm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
