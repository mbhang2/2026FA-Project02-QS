#!/usr/bin/env python3
"""FWER for `null_source: random_trader` on REAL NQ, train split only.

    python scripts/nq_rt_fwer.py --reps 250 --table 8000

Completes the licence for the default null on the one instrument that matters.
`random_trader` was calibrated on the SPX Heston path (FWER 0.057 at N=20,
0.043 at N=50) and shown on NQ to be non-degenerate and not drift-rewarding --
buy-and-hold over a +25.7% split scored p = 0.2674 -- but the NQ FWER cells
themselves were never measured. A default validated on one dataset is not
validated.

**Train split only, read from the pre-registered calendar.** The holdout is not
touched: its dates are excluded by date, not by position, so a cache that gains
files cannot silently pull them in. `NQDataset` keys its cache on bucket ETags
and this session has two of the four credentials, so the price paths are read
from the committed .npz files directly -- which is all this null consumes.

Sessions are downsampled to a fixed grid. The construction places timestamps as
a FRACTION of the session, so resolution beyond a few hundred points per day
cannot change a draw, and 384 full NQ sessions do not fit in memory here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.scoring.correction import max_of_n                      # noqa: E402
from src.core.scoring.randomization.random_trader import (            # noqa: E402
    TraderSpec, build_table)

CACHE = ROOT / "data" / "cache" / "sessions" / "NQ"


def train_days(grid: int) -> list[np.ndarray]:
    import yaml

    cal = yaml.safe_load((ROOT / "config" / "calendars" / "nq-trades.yaml").read_text())
    tr = cal["splits"]["train"]
    lo, hi = tr["start"], tr["end"]
    if isinstance(lo, str):
        lo, hi = dt.date.fromisoformat(lo), dt.date.fromisoformat(hi)
    out = []
    for f in sorted(CACHE.glob("*.npz")):
        d = dt.date.fromisoformat(f.name[:10])
        if not (lo <= d <= hi):
            continue
        with np.load(f, allow_pickle=False) as z:
            px = z["price"]
        if px.size < 50:
            continue
        idx = np.linspace(0, px.size - 1, min(grid, px.size)).astype(int)
        out.append(px[idx].astype(float))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reps", type=int, default=250)
    ap.add_argument("--table", type=int, default=8000)
    ap.add_argument("--grid", type=int, default=500)
    ap.add_argument("--comm", type=float, default=0.10)
    ap.add_argument("--q", type=float, default=0.05)
    a = ap.parse_args()

    days = train_days(a.grid)
    print(f"  NQ train split: {len(days)} sessions, {a.grid}-point grid, "
          f"commission {a.comm} pts/side", flush=True)
    print(f"  building the shared table, {a.table:,} draws ...", flush=True)
    tab = build_table(days, TraderSpec(iterations=a.table, seed=17,
                                       commission_per_contract=a.comm),
                      dataset="nq-trades:NQ", split="train")
    draws = np.asarray(tab.draws)
    print(f"    {draws.size:,} draws, {tab.round_turns_per_session:.1f} round "
          f"turns/session, {tab.share_above_zero:.1%} positive, "
          f"degenerate={tab.degenerate}", flush=True)
    print()

    rng = np.random.default_rng(23)
    print(f"  {'cell':34}{'FWER':>8}{'95% CI':>18}", flush=True)
    cells = (("N=20, candidate matches table", 20, 14.0),
             ("N=50, candidate matches table", 50, 14.0),
             ("N=50, candidate 0.7x activity", 50, 10.0),
             ("N=50, candidate 2x activity", 50, 28.0))
    for label, n, mean in cells:
        hits = tot = 0
        for _ in range(a.reps):
            obs = np.asarray(build_table(
                days, TraderSpec(iterations=n, mean_timestamps=mean,
                                 commission_per_contract=a.comm,
                                 seed=int(rng.integers(1, 10 ** 9)))).draws)
            if obs.size == 0:
                continue
            r = max_of_n([list(draws)] * obs.size, float(obs.max()),
                         b=min(draws.size, 10_000), q=a.q)
            hits += int(r["reject"])
            tot += 1
        f = hits / tot if tot else float("nan")
        se = (f * (1 - f) / tot) ** 0.5 if tot else float("nan")
        lo, hi = max(0.0, f - 1.96 * se), min(1.0, f + 1.96 * se)
        flag = "" if hi <= a.q + 0.02 else "   <-- above q"
        print(f"  {label:34}{f:>8.3f}   [{lo:.3f}, {hi:.3f}]{flag}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
