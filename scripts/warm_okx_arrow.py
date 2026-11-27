#!/usr/bin/env python3
"""Write immutable Arrow trade files for OKX train and validation.

    python scripts/warm_okx_arrow.py --check
    python scripts/warm_okx_arrow.py --limit 4 --workers 2
    python scripts/warm_okx_arrow.py --workers 3

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
sys.path.insert(0, str(ROOT / "scripts"))

import warm_okx_depth as depth  # noqa: E402


def _warm(days: list) -> int:
    dataset = depth._dataset()
    for day in days:
        dataset.warm_trade_store(day)
    return len(days)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    dataset = depth._dataset()
    days = depth.wanted(dataset)
    missing = [d for d in days if not dataset.has_trade_store(d)]
    print(f"{len(days)} sessions, {len(missing)} without an Arrow trade file", flush=True)
    if a.check:
        return 1 if missing else 0
    if a.limit is not None:
        missing = missing[:a.limit]
    t0, done = time.time(), 0
    batches = [missing[i:i + 8] for i in range(0, len(missing), 8)]
    with ProcessPoolExecutor(a.workers) as pool:
        for future in as_completed([pool.submit(_warm, b) for b in batches]):
            done += future.result()
            if done % 80 == 0 or done == len(missing):
                print(f"{done}/{len(missing)}  {done / max(time.time() - t0, 1e-9) * 60:.1f}/min", flush=True)
    left = sum(not dataset.has_trade_store(d) for d in days)
    print(f"sessions still without an Arrow trade file: {left}", flush=True)
    return 0 if a.limit is not None or not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
