#!/usr/bin/env python3
"""Record which contract each NQ session's ticks come from.

    python scripts/nq_contracts.py                      # every session
    python scripts/nq_contracts.py --from 2025-08-01 --to 2025-10-01

`stype_in=PARENT` means every NQ contract is in the file. Choosing one is a
research convention, not a fact, so the choice is written down per session --
a roll is then visible as data rather than inferred from a chart, and a wrong
rule shows up as a contract that changes on the wrong day or flickers.

Writes `data/nq-contract-selection.json`, merging with what is already there.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import inventory as inv  # noqa: E402

from src.core.data.nq import NQDataset  # noqa: E402

OUT = REPO_ROOT / "data" / "nq-contract-selection.json"

# A session is flagged `near_roll` when the chosen contract holds less than this
# share of session volume -- i.e. the rule had a real competitor. Measured, not
# guessed: outside the roll weeks the minimum share is 0.90 and the median 0.998,
# so every session below the line is within four calendar days of a roll. Stored
# rather than left to each strategy to re-derive, because a threshold that lives
# in strategy code is a threshold that differs between strategies.
NEAR_ROLL_SHARE = 0.90


def near_roll(row: dict) -> bool:
    return row["share_of_session_volume"] < NEAR_ROLL_SHARE


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", type=dt.date.fromisoformat)
    ap.add_argument("--to", dest="stop", type=dt.date.fromisoformat)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    import os

    ds = NQDataset(inv.s3_client(12), os.environ["R2_BUCKET"])
    sessions = [
        d for d in ds.sessions()
        if (args.start is None or d >= args.start) and (args.stop is None or d <= args.stop)
    ]
    existing = json.loads(args.out.read_text())["sessions"] if args.out.exists() else {}

    t0 = time.monotonic()
    for i, d in enumerate(sessions, 1):
        key = d.isoformat()
        if key in existing:
            continue
        ds.load(d)
        existing[key] = ds.contract_for(d).as_dict()
        if i % 20 == 0:
            print(f"  {i}/{len(sessions)}  {key}  {existing[key]['raw_symbol']}  "
                  f"({time.monotonic() - t0:.0f}s)", flush=True)

    ordered = dict(sorted(existing.items()))
    # Derived at write time, so it is applied to rows recorded by earlier runs
    # too -- a flag that only exists on newly fetched sessions is worse than no
    # flag, because it reads as False on the old ones.
    for row in ordered.values():
        row["near_roll"] = near_roll(row)
    rolls = []
    prev = None
    for k, v in ordered.items():
        if prev and v["raw_symbol"] != prev[1]["raw_symbol"]:
            rolls.append({"session": k, "from": prev[1]["raw_symbol"],
                          "to": v["raw_symbol"],
                          "share_before": prev[1]["share_of_session_volume"],
                          "share_after": v["share_of_session_volume"]})
        prev = (k, v)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "note": ("Front-month selection per session. The rule is a research "
                 "convention, not a fact: highest traded volume among outright "
                 "contracts in the session. Recorded so a roll is data."),
        "rule": "highest traded volume among outright contracts in the session",
        "sessions_recorded": len(ordered),
        "near_roll_rule": (
            f"share_of_session_volume < {NEAR_ROLL_SHARE}: the chosen contract "
            "had a real competitor in that session. Every flagged session is "
            "within four calendar days of a roll."
        ),
        "near_roll_sessions": sum(1 for v in ordered.values() if v["near_roll"]),
        "rolls": rolls,
        "sessions": ordered,
    }, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out.relative_to(REPO_ROOT)}: {len(ordered)} sessions, "
          f"{len(rolls)} roll(s), "
          f"{sum(1 for v in ordered.values() if v['near_roll'])} near-roll, "
          f"{time.monotonic() - t0:.0f}s")
    for r in rolls:
        print(f"  {r['session']}  {r['from']} -> {r['to']}  "
              f"share {r['share_before']:.3f} -> {r['share_after']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
