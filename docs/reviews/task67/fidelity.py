"""Task 67 — what resolution does a volume profile need?

`config/hypotheses/auction-market-features.md` leaves this open and says the
answer gets written back into it before trial 0. Ticks are exact; fine time bars
are cheaper. This measures how much fidelity each resolution actually costs, on
real NQ sessions, against the tick-built profile as ground truth.

Fidelity only. The cost half is `cost.py`, which needs the engine.
"""
from __future__ import annotations

import json, os, pathlib, sys, time
from collections import defaultdict

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from src.core.features.volume_profile import SessionProfile   # noqa: E402
from src.core.types import Split                              # noqa: E402

SESSIONS = 5
TICK_SIZE = 0.25
INTERVALS_NS = {"1s": 1_000_000_000, "5s": 5_000_000_000, "30s": 30_000_000_000}


def bar_profile(ticks, interval_ns, session_open_ts):
    """Volume at price from bars: each bar's volume spread evenly over the price
    levels its own high-low range touched.

    The approximation under test. At one second an NQ bar spans a level or three;
    at thirty it spans many, and the even spread stops describing anything.
    """
    bars = defaultdict(lambda: {"lo": None, "hi": None, "vol": 0})
    for t in ticks:
        k = (t.ts - session_open_ts) // interval_ns
        b = bars[k]
        b["lo"] = t.price if b["lo"] is None else min(b["lo"], t.price)
        b["hi"] = t.price if b["hi"] is None else max(b["hi"], t.price)
        b["vol"] += t.size
    p = SessionProfile(TICK_SIZE)
    for b in bars.values():
        lo = int(round(b["lo"] / TICK_SIZE)); hi = int(round(b["hi"] / TICK_SIZE))
        levels = hi - lo + 1
        share, rem = divmod(b["vol"], levels)
        for i, k in enumerate(range(lo, hi + 1)):
            v = share + (1 if i < rem else 0)
            if v:
                p.add(k * TICK_SIZE, v)
    return p


def main() -> None:
    from src.core.data.nq import NQDataset
    ds = NQDataset()
    print(f"dataset {ds.name}")

    by_session: dict[int, list] = defaultdict(list)
    t0 = time.monotonic()
    for t in ds.ticks(Split.TRAIN):
        if len(by_session) > SESSIONS and t.session not in by_session:
            break
        by_session[t.session].append(t)
    read_s = time.monotonic() - t0
    sids = sorted(by_session)[:SESSIONS]
    total = sum(len(by_session[s]) for s in sids)
    print(f"read {total:,} ticks over {len(sids)} sessions in {read_s:.1f}s "
          f"({total/len(sids):,.0f}/session)")

    rows = []
    for sid in sids:
        ticks = by_session[sid]
        open_ts = ticks[0].ts
        truth = SessionProfile(TICK_SIZE)
        t0 = time.monotonic()
        for t in ticks:
            truth.add(t.price, t.size)
        tick_s = time.monotonic() - t0
        tv, tlo, thi = truth.vpoc(), *truth.value_area()
        row = {"session": sid, "ticks": len(ticks), "tick_build_s": tick_s,
               "vpoc": tv, "va_low": tlo, "va_high": thi,
               "lvn": len(truth.low_volume_nodes())}
        for name, ns in INTERVALS_NS.items():
            t0 = time.monotonic()
            p = bar_profile(ticks, ns, open_ts)
            row[f"{name}_build_s"] = time.monotonic() - t0
            v, lo, hi = p.vpoc(), *p.value_area()
            row[f"{name}_vpoc_err_ticks"] = abs(v - tv) / TICK_SIZE
            row[f"{name}_va_low_err_ticks"] = abs(lo - tlo) / TICK_SIZE
            row[f"{name}_va_high_err_ticks"] = abs(hi - thi) / TICK_SIZE
            row[f"{name}_lvn"] = len(p.low_volume_nodes())
        rows.append(row)
        print(f"  session {sid}: {len(ticks):,} ticks  vpoc {tv:.2f}  "
              f"VA [{tlo:.2f}, {thi:.2f}]")

    out = {"sessions": len(rows), "tick_size": TICK_SIZE, "rows": rows}
    for name in INTERVALS_NS:
        for k in ("vpoc", "va_low", "va_high"):
            errs = [r[f"{name}_{k}_err_ticks"] for r in rows]
            out[f"{name}_{k}_err_ticks_mean"] = sum(errs) / len(errs)
            out[f"{name}_{k}_err_ticks_max"] = max(errs)
    (pathlib.Path(__file__).parent / "fidelity.json").write_text(
        json.dumps(out, indent=2) + "\n")

    print("\nerror against the tick-built profile, in TICKS (0.25 index points):")
    print(f"  {'':6} {'VPOC mean':>10} {'VPOC max':>9} {'VAlow mean':>11} "
          f"{'VAhigh mean':>12}")
    for name in INTERVALS_NS:
        print(f"  {name:6} {out[f'{name}_vpoc_err_ticks_mean']:>10.2f} "
              f"{out[f'{name}_vpoc_err_ticks_max']:>9.2f} "
              f"{out[f'{name}_va_low_err_ticks_mean']:>11.2f} "
              f"{out[f'{name}_va_high_err_ticks_mean']:>12.2f}")


if __name__ == "__main__":
    main()
