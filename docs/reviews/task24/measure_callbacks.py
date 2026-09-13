#!/usr/bin/env python3
"""Measurement 1 and 2: what would actually cross a process boundary.

Answered on the existing in-process engine, with no IPC written. A proxy context
counts every attribute the strategy reads and every `ctx.history(...)` it pulls,
and the strategy wrapper counts callbacks. Nothing about the engine changes -- the
wrapper delegates to the real reference strategy and the proxy forwards every
attribute -- so the blotter is asserted identical to an uninstrumented run.

    python docs/reviews/task24/measure_callbacks.py --sessions 40      # synthetic
    python docs/reviews/task24/measure_callbacks.py --nq               # real NQ
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from collections import Counter

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from src.core.engine import Engine  # noqa: E402
from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.types import Split  # noqa: E402

# What one bar costs on the wire, as nine JSON fields. Measured rather than
# guessed: see `bar_payload_bytes` below.
COUNTS: Counter = Counter()
HISTORY_LEN: Counter = Counter()
SLICE_LEN: Counter = Counter()


class CountingHistory:
    """The window a `ctx.history(...)` returned, counting what was *read* from it.

    The distinction is the whole question. In-process `ctx.history()` hands back a
    lazy view over everything the run has seen -- 810 bars on average by the end
    of a synthetic split -- and the reference strategy reads exactly one of them.
    Across a boundary, "what the call returns" and "what the strategy looks at"
    become different costs, and only the second is irreducible.
    """

    __slots__ = ("_view",)

    def __init__(self, view):
        object.__setattr__(self, "_view", view)

    def __len__(self):
        COUNTS["hist:len"] += 1
        return len(self._view)

    def __getitem__(self, key):
        out = self._view[key]
        if isinstance(key, slice):
            n = len(out)
            COUNTS["hist:bars_read"] += n
            COUNTS["hist:slices"] += 1
            SLICE_LEN[n] += 1
        else:
            COUNTS["hist:bars_read"] += 1
            COUNTS["hist:index"] += 1
        return out

    def __iter__(self):
        COUNTS["hist:iter"] += 1
        for bar in self._view:
            COUNTS["hist:bars_read"] += 1
            yield bar

    def __getattr__(self, name):
        COUNTS[f"hist:attr:{name}"] += 1
        return getattr(self._view, name)


class CountingCtx:
    """Forwards everything, counts what was asked for.

    A proxy rather than an engine change: the spike must not touch semantics, and
    a wrapper that only reads is the cheapest way to be sure it did not.
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx):
        object.__setattr__(self, "_ctx", ctx)

    def __getattr__(self, name):
        value = getattr(self._ctx, name)
        if callable(value):
            def counted(*a, **kw):
                COUNTS[f"call:{name}"] += 1
                out = value(*a, **kw)
                if name == "history":
                    try:
                        HISTORY_LEN[len(out)] += 1
                    except TypeError:
                        pass
                    return CountingHistory(out)
                return out
            return counted
        COUNTS[f"read:{name}"] += 1
        return value


class Instrumented:
    """The reference strategy, wrapped. Same decisions, counted."""

    def __init__(self):
        self.inner = ReferenceStrategy()

    def setup(self, ctx):
        COUNTS["cb:setup"] += 1
        return self.inner.setup(CountingCtx(ctx))

    def on_bars(self, ctx):
        COUNTS["cb:on_bars"] += 1
        COUNTS["bars_in_callback"] += len(ctx.closed)
        return self.inner.on_bars(CountingCtx(ctx))

    def on_fill(self, ctx):
        COUNTS["cb:on_fill"] += 1
        return self.inner.on_fill(CountingCtx(ctx))

    def on_lifecycle(self, ctx):
        COUNTS["cb:on_lifecycle"] += 1
        return self.inner.on_lifecycle(CountingCtx(ctx))


def bar_payload_bytes() -> int:
    """One `Bar` as a JSON array of its nine fields, with realistic magnitudes."""
    row = ["time:300000000000", 1_767_225_600_000_000_000, 1_767_225_900_000_000_000,
           21_345.25, 21_352.75, 21_340.00, 21_349.50, 1_284, 96, 739_252]
    return len(json.dumps(row))


def build(nq: bool, sessions: int, cold: bool):
    if nq:
        import os

        import boto3

        from src.core.data.nq import NQDataset, NQSpec
        client = boto3.client(
            "s3", endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
        return NQDataset(client, os.environ["R2_BUCKET"], NQSpec(cache=not cold))
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    return SyntheticDataset(SyntheticSpec(n_sessions=sessions, ticks_per_session=390,
                                          vol_per_tick=0.15, seed=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nq", action="store_true")
    ap.add_argument("--sessions", type=int, default=200)
    ap.add_argument("--cold", action="store_true")
    ap.add_argument("--project-to", type=int, default=564)
    args = ap.parse_args()

    dataset = build(args.nq, args.sessions, args.cold)

    # the uninstrumented run, as the control
    t0 = time.perf_counter()
    plain = Engine.for_test(dataset, ReferenceStrategy(), split=Split.VALIDATION,
                            warmup_sessions=1, fill_model="unfavorable", seed=1)
    plain.run()
    plain_wall = time.perf_counter() - t0

    dataset2 = build(args.nq, args.sessions, args.cold)
    t0 = time.perf_counter()
    engine = Engine.for_test(dataset2, Instrumented(), split=Split.VALIDATION,
                             warmup_sessions=1, fill_model="unfavorable", seed=1)
    record = engine.run()
    instrumented_wall = time.perf_counter() - t0

    assert engine.blotter.digest() == plain.blotter.digest(), (
        "the instrumentation changed behaviour; every number below is about a "
        "different run than the one the harness scores"
    )

    scored = len(record.end_of_session_positions)
    callbacks = {k[3:]: v for k, v in COUNTS.items() if k.startswith("cb:")}
    total_callbacks = sum(callbacks.values())
    reads = {k[5:]: v for k, v in COUNTS.items() if k.startswith("read:")}
    calls = {k[5:]: v for k, v in COUNTS.items() if k.startswith("call:")}
    history_calls = calls.get("history", 0)
    bars_pulled = sum(n * c for n, c in HISTORY_LEN.items())
    per_bar = bar_payload_bytes()

    scale = args.project_to / scored

    print(f"dataset            {dataset.name}")
    print(f"sessions scored    {scored}   (projecting x{scale:.2f} to "
          f"{args.project_to})")
    print(f"ticks              {record.ticks:,}")
    print(f"wall               {plain_wall:.2f}s plain, "
          f"{instrumented_wall:.2f}s instrumented")
    print(f"blotter            {engine.blotter.digest()[:16]}  (identical to plain)")
    print()
    print("1. CALLBACK VOLUME  -- one message each, engine -> strategy")
    for name, n in sorted(callbacks.items(), key=lambda kv: -kv[1]):
        print(f"   {name:<14} {n:>10,}   -> {n * scale:>12,.0f} at "
              f"{args.project_to} sessions")
    print(f"   {'TOTAL':<14} {total_callbacks:>10,}   -> "
          f"{total_callbacks * scale:>12,.0f}")
    print(f"   bars delivered inside those callbacks: "
          f"{COUNTS['bars_in_callback']:,}")
    print()
    print("2. ctx.history VOLUME  -- one round trip each, if pulls are served")
    print(f"   history() calls       {history_calls:>10,}   -> "
          f"{history_calls * scale:>12,.0f}")
    print(f"   bars returned         {bars_pulled:>10,}   -> "
          f"{bars_pulled * scale:>12,.0f}")
    if history_calls:
        print(f"   mean window returned  {bars_pulled / history_calls:>10.1f} bars")
        print(f"   window size histogram (top 8): "
              f"{sorted(HISTORY_LEN.items(), key=lambda kv: -kv[1])[:8]}")
    print(f"   one bar on the wire   {per_bar:>10,} bytes (nine JSON fields)")
    print(f"   naive pull payload    {bars_pulled * per_bar / 1e6:>10.2f} MB   -> "
          f"{bars_pulled * per_bar * scale / 1e6:>9.1f} MB")
    print()
    print("2b. WHAT WAS ACTUALLY READ from those windows")
    read = COUNTS["hist:bars_read"]
    print(f"   bars read             {read:>10,}   -> {read * scale:>12,.0f}")
    print(f"   as {COUNTS['hist:index']:,} single indexes, "
          f"{COUNTS['hist:slices']:,} slices, {COUNTS['hist:iter']:,} iterations")
    print(f"   len() without reading {COUNTS['hist:len']:>10,}")
    if bars_pulled:
        print(f"   read / returned       {read / bars_pulled:>10.5f}  "
              f"<- the ratio the protocol decision turns on")
    print(f"   irreducible payload   {read * per_bar / 1e6:>10.4f} MB   -> "
          f"{read * per_bar * scale / 1e6:>9.3f} MB")
    if SLICE_LEN:
        print(f"   slice sizes (top 8): "
              f"{sorted(SLICE_LEN.items(), key=lambda kv: -kv[1])[:8]}")
    print()
    print("   other context calls:", dict(sorted(calls.items(), key=lambda kv: -kv[1])))
    print("   context attribute reads:",
          dict(sorted(reads.items(), key=lambda kv: -kv[1])))
    json.dump(
        {
            "dataset": dataset.name, "sessions": scored, "ticks": record.ticks,
            "project_to": args.project_to, "scale": scale,
            "callbacks": callbacks, "total_callbacks": total_callbacks,
            "history_calls": history_calls, "bars_pulled": bars_pulled,
            "window_histogram": {str(k): v for k, v in HISTORY_LEN.items()},
            "reads": reads, "calls": calls,
            "bars_read": COUNTS["hist:bars_read"],
            "history_index_reads": COUNTS["hist:index"],
            "history_slices": COUNTS["hist:slices"],
            "history_iterations": COUNTS["hist:iter"],
            "history_len_calls": COUNTS["hist:len"],
            "slice_histogram": {str(k): v for k, v in SLICE_LEN.items()},
            "bar_payload_bytes": per_bar,
            "wall_plain_s": plain_wall,
        },
        (pathlib.Path(__file__).parent /
         f"callbacks-{'nq' if args.nq else 'synthetic'}.json").open("w"),
        indent=2, sort_keys=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
