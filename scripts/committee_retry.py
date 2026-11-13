#!/usr/bin/env python3
"""Re-run the committee seats that never ran, beside the record and never over it.

    python scripts/committee_retry.py --campaign nq-t72b-01
    python scripts/committee_retry.py --all --dry-run
    python scripts/committee_retry.py --campaign nq-s04-03 --axes economics

**Why this exists.** Of the 27 committees on record, nine economics seats never
ran -- three gateway 502s, three replies that were not JSON, three that spent the
whole token budget thinking. The economics seat is the one that asks whether the
champion implements the question it was registered under, so one campaign in
three was never checked for that. Two of the nine were re-run by hand on
2026-10-10 and one of those came back VETO for exactly that reason. This makes
the hand procedure a command.

**What it writes.** `committee-seat-retry.json` in the campaign directory, schema
`committee-verdict/1-partial`, the shape the 2026-10-10 hand retries used. The
committed `committee-verdict.json` is never touched: it records what the
campaign's committee actually did, and a seat that failed then did fail then.
A later read cannot change the recorded verdict and does not claim to. If a
retry file already exists, seats it already resolved are kept and only the
still-unresolved ones are asked again.

**What the seat is shown** is what `nq_campaign.py --stage report` showed the
original committee: the same `tier2-summary.json`, the same validation and
buy-and-hold attachments, the same hypothesis, through the same
`committee_evidence()` -- plus the best trial's stored source when the summary
carried none, which is the one difference and is recorded in the file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

CAMPAIGNS = ROOT / "runs" / "campaigns"
RETRY_FILE = "committee-seat-retry.json"
RESOLVED = ("PASS", "VETO")


def failed_seats(verdict: dict) -> list[str]:
    """Axes whose auditor never produced a verdict, from a committed record.

    Read off the reasons rather than `seats_failed`, because verdicts written
    before that field existed are most of the record. A whole-committee ERROR
    means no seat ran.
    """
    from audit.committee.review import AXES

    if (verdict.get("verdict") or "").upper() == "ERROR":
        return list(AXES)
    out = []
    for a in verdict.get("auditors") or []:
        reasons = " ".join(str(r) for r in (a.get("reasons") or []))
        if "did not run" in reasons or (a.get("verdict") == "CANNOT_TELL"
                                        and a.get("error")):
            out.append(a.get("axis"))
    return out


def resolved_in_retry(d: pathlib.Path) -> dict:
    f = d / RETRY_FILE
    if not f.exists():
        return {}
    seats = json.loads(f.read_text()).get("seats") or {}
    return {k: v for k, v in seats.items() if v.get("verdict") in RESOLVED}


def pending(d: pathlib.Path, axes: list[str] | None) -> list[str]:
    cv = d / "committee-verdict.json"
    if not cv.exists():
        return []
    want = axes or failed_seats(json.loads(cv.read_text()))
    done = resolved_in_retry(d)
    return [a for a in want if a not in done]


def retry(name: str, axes: list[str] | None, *, dry_run: bool) -> dict | None:
    d = CAMPAIGNS / name
    todo = pending(d, axes)
    if not todo:
        print(f"{name}: nothing to re-run")
        return None
    print(f"{name}: re-running {', '.join(todo)}")
    if dry_run:
        return None

    import nq_campaign as nc
    from audit.committee.review import review
    from src.core.loop.campaign import CampaignRecord

    record, _ = CampaignRecord.read(d / "campaign.json")
    tier2 = json.loads((d / "tier2-summary.json").read_text())
    summary_had_source = bool((tier2.get("best_candidate") or {}).get("source"))
    hyp_text, source = nc.committee_evidence(d, record, tier2)
    proposer = (record.agent_config or {}).get("model", nc.MODEL)
    cv = review(tier2, source=source, hypothesis=hyp_text,
                proposer_model=proposer, axes=tuple(todo))

    f = d / RETRY_FILE
    out = json.loads(f.read_text()) if f.exists() else {
        "schema": "committee-verdict/1-partial",
        "note": ("Seats re-run after the campaign's committee, because they "
                 "never produced a verdict in committee-verdict.json. Written "
                 "BESIDE that file and never over it: the committed artefact "
                 "records what the campaign's committee actually did, and a "
                 "seat that failed then did fail then. This is a later, "
                 "separate read; it cannot change the recorded verdict and "
                 "does not."),
        "can_approve": False,
        "seats": {},
    }
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    for a in cv.auditors:
        out["seats"][a.axis] = {
            "verdict": a.verdict,
            "reasons": list(a.reasons),
            "confidence_note": a.confidence_note,
            "model": cv.model,
            "model_served": cv.model_served,
            "independent": cv.independent,
            "attempts": a.attempts,
            "rerun_on": today,
            "source_from": ("tier2-summary" if summary_had_source
                            else "ledger best trial" if source else "none"),
            **({"error": a.error} if a.error else {}),
        }
    out["prompt_digests"] = cv.prompt_digests
    out.setdefault("usage", []).extend(cv.usage)
    f.write_text(json.dumps(out, indent=2, default=str) + "\n")
    for a in cv.auditors:
        print(f"    {a.axis:<12} {a.verdict}  ({a.attempts} attempt(s))")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--campaign", action="append", default=[])
    g.add_argument("--all", action="store_true",
                   help="every campaign with a seat that never ran")
    ap.add_argument("--axes", nargs="+", default=None,
                    help="re-run these seats instead of the ones that failed")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    names = a.campaign or sorted(p.name for p in CAMPAIGNS.iterdir()
                                 if (p / "committee-verdict.json").exists())
    for name in names:
        if not (CAMPAIGNS / name / "committee-verdict.json").exists():
            print(f"{name}: no committee-verdict.json; run --stage report first")
            continue
        retry(name, a.axes, dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
