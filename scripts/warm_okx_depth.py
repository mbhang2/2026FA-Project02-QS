#!/usr/bin/env python3
"""Store OKX book levels 2 to 5 for train and validation, so reads need no decode.

    python scripts/warm_okx_depth.py --check
    python scripts/warm_okx_depth.py --limit 4 --workers 2
    python scripts/warm_okx_depth.py --workers 3

Writes only the local cache. The holdout is never read here.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROFILES = ("btc-okx-hft-train", "btc-okx-hft-validation")


def _dataset():
    import run

    cfg = run.load_config()
    profile = cfg["profiles"][PROFILES[0]]
    return run.build_dataset(cfg, profile["dataset"], profile["seed"])


def _warm(days: list) -> int:
    dataset = _dataset()
    for day in days:
        dataset.warm_depth(day)
    return len(days)


def wanted(dataset) -> list:
    import run
    from src.core.types import Split

    cfg = run.load_config()
    out = []
    for name in PROFILES:
        profile = cfg["profiles"][name]
        out += dataset._days(Split(profile["split"]), profile["warmup_sessions"])
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    dataset = _dataset()
    days = wanted(dataset)
    missing = [d for d in days if not dataset.has_depth(d)]
    print(f"{len(days)} sessions, {len(missing)} without stored depth", flush=True)
    if a.check:
        return 1 if missing else 0
    if a.limit is not None:
        missing = missing[:a.limit]
    t0, done = time.time(), 0
    batches = [missing[i:i + 4] for i in range(0, len(missing), 4)]
    with ProcessPoolExecutor(a.workers) as pool:
        for future in as_completed([pool.submit(_warm, b) for b in batches]):
            done += future.result()
            rate = done / max(time.time() - t0, 1e-9)
            if done % 40 == 0 or done == len(missing):
                print(f"{done}/{len(missing)}  {rate * 60:.1f}/min", flush=True)
    left = sum(not dataset.has_depth(d) for d in days)
    print(f"sessions still without depth: {left}", flush=True)
    return 0 if a.limit is not None or not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
