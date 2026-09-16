#!/usr/bin/env python3
"""Item 0: what does `setup()` actually have to send across the boundary?

    python docs/reviews/task25/measure_setup.py --sessions 40

The Task 24 spike sent an empty setup message and named this the one unmeasured
payload, with an estimate of 22 GiB. That estimate is for `ctx.ticks()`, which
materialises every trade in the training split. The question this answers is
whether any strategy needs that, or whether it is an artifact of what the API
happens to hand over.

Measured on the **training** split only -- the one split a strategy is handed in
bulk by design. Bounded to `--sessions` so nothing here downloads or materialises
the whole thing: the per-session numbers are what project, and projecting from a
measured rate is the honest version of a number that cannot be run in full on a
laptop.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from src.core.engine.engine import aggregate  # noqa: E402
from src.core.types import Split, Subscription  # noqa: E402
from src.core.engine.context import parse_interval  # noqa: E402

TRAIN_SESSIONS_NQ = 384          # config/calendars/nq-trades.yaml, pre-registered


def nq_dataset():
    import boto3

    from src.core.data.nq import NQDataset, NQSpec
    client = boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    return NQDataset(client, os.environ["R2_BUCKET"], NQSpec())


def bounded(ticks, limit: int):
    """The first `limit` sessions of the stream, and the tick count."""
    seen, session, count = 0, None, 0
    for tick in ticks:
        if tick.session != session:
            session = tick.session
            seen += 1
            if seen > limit:
                return
        count += 1
        yield tick


def tick_row(t) -> list:
    return [t.ts, t.seq, t.price, t.size, t.session]


def bar_row(b) -> list:
    return [b.sub_id, b.open_ts, b.close_ts, b.open, b.high, b.low, b.close,
            b.volume, b.tick_count, b.session]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=40,
                    help="training sessions to actually walk")
    ap.add_argument("--interval", default="5m")
    args = ap.parse_args()

    ds = nq_dataset()
    print(f"dataset   {ds.name}   training split, first {args.sessions} sessions")

    # --- what ctx.ticks() would have to send -------------------------------- #
    t0 = time.perf_counter()
    n_ticks, tick_bytes, sessions_seen = 0, 0, set()
    for tick in bounded(ds.ticks(Split.TRAIN), args.sessions):
        n_ticks += 1
        sessions_seen.add(tick.session)
        if n_ticks <= 10_000:                 # sample the encoding, not all of it
            tick_bytes += len(json.dumps(tick_row(tick)))
    walked = len(sessions_seen)
    per_tick = tick_bytes / min(n_ticks, 10_000)
    tick_wall = time.perf_counter() - t0

    # --- what ctx.train_bars(interval) would have to send -------------------- #
    sub = Subscription(interval_ns=parse_interval(args.interval))
    t0 = time.perf_counter()
    bars = aggregate(bounded(ds.ticks(Split.TRAIN), args.sessions),
                     sub, ds.session_bounds)
    bar_wall = time.perf_counter() - t0
    bar_bytes = sum(len(json.dumps(bar_row(b))) for b in bars)

    scale = TRAIN_SESSIONS_NQ / walked
    print(f"walked    {walked} sessions in {tick_wall:.1f}s "
          f"(+{bar_wall:.1f}s to aggregate)")
    print()
    print(f"{'':<28}{'measured':>16}{'x' + str(round(scale, 2)):>10}"
          f"{'full 384-session train':>26}")
    print(f"{'ctx.ticks() events':<28}{n_ticks:>16,}{'':>10}"
          f"{n_ticks * scale:>26,.0f}")
    print(f"{'ctx.ticks() payload':<28}{n_ticks * per_tick / 1e6:>15,.0f}MB{'':>10}"
          f"{n_ticks * per_tick * scale / 1e9:>24,.1f}GB")
    print(f"{'train_bars(' + args.interval + ') bars':<28}{len(bars):>16,}{'':>10}"
          f"{len(bars) * scale:>26,.0f}")
    print(f"{'train_bars(' + args.interval + ') payload':<28}"
          f"{bar_bytes / 1e6:>15,.2f}MB{'':>10}"
          f"{bar_bytes * scale / 1e6:>24,.1f}MB")
    print()
    ratio = (n_ticks * per_tick) / max(bar_bytes, 1)
    print(f"ticks / bars payload ratio   {ratio:,.0f}x")

    out = {
        "dataset": ds.name, "sessions_walked": walked,
        "train_sessions": TRAIN_SESSIONS_NQ, "scale": scale,
        "interval": args.interval,
        "ticks": n_ticks, "tick_bytes_each": per_tick,
        "ticks_full_bytes": n_ticks * per_tick * scale,
        "bars": len(bars), "bars_bytes": bar_bytes,
        "bars_full_bytes": bar_bytes * scale,
        "ratio": ratio,
        "tick_wall_s": tick_wall, "bar_wall_s": bar_wall,
    }
    (pathlib.Path(__file__).parent / "setup-payload.json").write_text(
        json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
