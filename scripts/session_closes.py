#!/usr/bin/env python3
"""Record when each early-closing session actually ends.

    python scripts/session_closes.py

The manifest says *which* dates close early; it does not say *when*, because the
inventory reads headers rather than contents. Without the times the adapter
computes every session as running to the normal 16:00 CT close, so on a half day
a strategy is told it has four more hours than the data has -- which is why 12 of
384 NQ training sessions stranded a position and why the null pool excluded 25 of
them as a workaround.

Two sources, and they are kept separate on purpose:

* the **exchange calendar** (`pandas_market_calendars`), which is published in
  advance and is therefore the only source a strategy may legitimately act on;
* the **observed last trade**, which is data and is here to check the calendar,
  never to predict.

Where they disagree the file records both and the resolved end is the **later**
of the two. Later rather than earlier because the session end also bounds which
ticks belong to the session: taking the calendar's word on a day when trading
demonstrably continued past it would silently discard real trades, and a
calibration pool quietly missing the last quarter-hour of five sessions is worse
than a session that is declared fifteen minutes longer than the calendar thinks.

Reading the observed close is not lookahead. It happens here, once, at build
time, and what reaches a trial is a fixed schedule -- the same standing as the
pre-registered split calendar.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import inventory as inv  # noqa: E402

from src.core.data.nq import NQDataset, NQSpec  # noqa: E402

CT = ZoneInfo("America/Chicago")
OUT = REPO_ROOT / "data" / "nq-session-closes.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    ds = NQDataset(inv.s3_client(8), os.environ["R2_BUCKET"], NQSpec())
    manifest = json.loads((REPO_ROOT / "data" / "manifest.json").read_text())
    calendar = manifest["instrument_coverage"]["NQ"]["calendar"]
    times = calendar.get("early_close_times_ct")
    if not times:
        sys.exit(
            "data/manifest.json carries no early_close_times_ct; re-run "
            "scripts/inventory.py first"
        )

    sessions = {d.isoformat() for d in ds.sessions()}
    rows, disagreements = {}, []
    t0 = time.monotonic()
    for date in sorted(times):
        if date not in sessions:
            continue
        ticks = ds.load(dt.date.fromisoformat(date))
        last = dt.datetime.fromtimestamp(
            ticks[-1].ts / 1e9, dt.timezone.utc
        ).astimezone(CT)
        observed = last.strftime("%H:%M:%S")
        stated = times[date]
        stated_dt = dt.datetime.combine(
            dt.date.fromisoformat(date),
            dt.time(*map(int, stated.split(":"))), tzinfo=CT,
        )
        # A trade in the final second of an interval is the close, not an
        # overrun: round the observed time up to the next whole minute before
        # comparing, so 11:59:59 reads as 12:00.
        observed_end = (last + dt.timedelta(seconds=1)).replace(microsecond=0)
        if observed_end.second:
            observed_end += dt.timedelta(seconds=60 - observed_end.second)
        resolved = max(stated_dt, observed_end)
        row = {
            "calendar_close_ct": stated,
            "observed_last_trade_ct": observed,
            "resolved_close_ct": resolved.strftime("%H:%M"),
            "ticks": len(ticks),
        }
        if resolved != stated_dt:
            row["disagreement_minutes"] = round(
                (resolved - stated_dt).total_seconds() / 60, 1
            )
            disagreements.append(date)
        rows[date] = row
        print(f"  {date}  calendar {stated}  observed {observed}  "
              f"-> {row['resolved_close_ct']}"
              + (f"   <-- {row['disagreement_minutes']:+.0f} min" if date in disagreements else ""),
              flush=True)

    doc = {
        "note": (
            "When each early-closing NQ session ends. The exchange calendar is "
            "the source a strategy may act on; the observed last trade is here to "
            "check it. Resolved end is the later of the two, so no observed trade "
            "is ever discarded by the session window."
        ),
        "normal_close_ct": calendar["normal_close_ct"],
        "dataset_digest": ds.digest(),
        "sessions": rows,
        "disagreements": disagreements,
        "disagreement_note": (
            "The calendar says 12:00 CT and trading demonstrably continued to "
            "12:15 on these dates -- the day after Thanksgiving, Christmas Eve, "
            "and one July 3. CME equity index futures close at 12:15 CT on those "
            "half days and pandas_market_calendars' CME_Equity reports 12:00. "
            "Reported rather than silently resolved: the resolved end is the "
            "later of the two so nothing is dropped, but the calendar is wrong "
            "about these dates and a CME-specific calendar would be the fix."
        ),
        "wall_clock_s": round(time.monotonic() - t0, 1),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {args.out.relative_to(REPO_ROOT)}: {len(rows)} early closes, "
          f"{len(disagreements)} disagreeing with the calendar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
