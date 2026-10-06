#!/usr/bin/env python3
"""The feedback edge: what came of the paper that suggested the question.

    python scripts/record_outcome.py --campaign nq-ml-07
    python scripts/record_outcome.py --all --dry-run

**The edge that was missing.** `docs/design/paper-retrieval.md` describes a loop:
a paper suggests a question, the question becomes a campaign, and the campaign's
result is recorded against the paper. The first three stages were built. The
last one was not, so `outcome` was `null` on all 284 rows and always had been --
the registry could say what we decided about a paper and never what came of it.
A pipeline whose results never return to it is a funnel, not a loop.

## What it writes, and the one thing it deliberately does not

On the paper's row: the campaign id appended to `campaign_ids`, and a record
appended to `outcome` carrying the **train-side verdicts** -- the tier-2 gate,
the tier-3 verdict, the committee's verdict and who vetoed, which stage stopped
it -- plus the campaign directory.

**No validation result either**, corrected 2026-09-29 after the first version
recorded one. §4: "Campaign k+1 may depend on prior campaigns' train results --
never on their validation results." The registry is read by the ranker, so a
validation result here is the same kind of door a score would be.

**No scores.** Not the best score, not the p-value, not the validation number.
That is not an oversight and it is the most important line in this file.
`rank_papers.py` is given campaign verdicts precisely *without* scores, because
ranking papers by resemblance to what scored well is selection on data already
spent -- and retrieval costs no N only because it never looks at our market
data. The registry is read by that ranker. A score written here would be a back
door into it, and the door would be invisible: the ranking would quietly become
a function of past performance and nothing in its output would say so.

The numbers live in `runs/campaigns/<id>/`, which this record points at. A human
following the pointer is fine. A ranker reading the registry cannot follow it.

## What it refuses to decide

**It does not update the falsified-families list.** One rejected campaign does
not falsify a family; the entries in `docs/datasets/nq-es-qqq.md` say things like
"tested seven ways, null". Promoting a single rejection to a closed family is a
judgement about weight of evidence, and automating it would let one bad campaign
close a line of research. It prints the suggestion and stops.

**It does not decide promotion.** No verdict in this project means approval, and
that is enforced in `audit/tier3.py` rather than by policy. `stopped_by` names a
stage or is `None`; `None` means the gate and the committee did not stop it, which
is neither a promotion nor a statement about validation.

## Where it runs

Automatically at the end of `nq_campaign.py --stage report`, which is where a
campaign concludes. That placement is deliberate and the lesson is recent: stage
0b was built as a standalone command, nobody triggered it, and the corpus
silently went stale. A feedback edge that has to be remembered is a feedback
edge that will not happen.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REGISTRY = ROOT / "data" / "papers" / "registry.json"
# Amendment 13: `runs/` unless a rehearsal redirects it. A rehearsal
# writes under `.scratch/`, which `runlog.ledger_paths()` does not read,
# so its trials are invisible to `global_n()` by construction.
import os as _os
_RUNS = pathlib.Path(_os.environ.get("PRACTICUM_RUNS_ROOT") or (ROOT / "runs"))
CAMPAIGNS = _RUNS / "campaigns"

# Everything this record may carry. A closed set, so adding a score means editing
# this list and reading the docstring above that says why not.
FIELDS = ("campaign_id", "recorded", "hypothesis", "gate", "tier3",
          "committee", "vetoed_by", "stopped_by", "campaign_dir")


def _read(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:                                            # noqa: BLE001
        return {}


def verdicts_for(name: str) -> dict | None:
    """Every stage's verdict for one campaign, or None if it has not concluded.

    "Concluded" means the gate ruled. A campaign still running, or one whose
    nulls have not been computed, has nothing to record and saying so is the
    correct answer.
    """
    d = CAMPAIGNS / name
    rec = _read(d / "campaign.json").get("record") or {}
    tier2 = _read(d / "tier2-summary.json")
    gate = (tier2.get("gate") or {}).get("verdict")
    if not gate:
        return None

    tier3 = _read(d / "tier3-verdict.json").get("verdict")
    com = _read(d / "committee-verdict.json")

    # **No validation result, and that is the correction of 2026-09-29.** The
    # first version recorded `validation_positive`. §4 is explicit: "Campaign k+1
    # may depend on prior campaigns' train results -- never on their validation
    # results. Choosing the next hypothesis by what did well on validation is
    # selecting on the thing being tested." `rank_papers.py` reads this registry,
    # so a validation result here sits one careless edit from the retrieval side,
    # which is the whole reason nothing here carries a score either.
    #
    # `stopped_by` therefore names only the TRAIN-side gate and the committee.
    # `None` means neither of those stopped it, which is NOT "it survived
    # validation" -- validation is deliberately not consulted here and the
    # campaign directory is where a human reads it.
    #
    # **The committee verdict is itself validation-informed**, because the report
    # stage hands the auditors a summary that embeds the validation numbers. So
    # dropping the field is necessary and not sufficient: the real protection is
    # that the ranker never reads a paper's `outcome` at all, which
    # `tests/test_rank_papers.py` now asserts.
    stopped_by = ("gate" if gate.upper() != "ACCEPT"
                  else "committee" if (com.get("verdict") or "").upper() == "VETO"
                  else None)
    return {
        "campaign_id": name,
        "recorded": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "hypothesis": rec.get("hypothesis_path"),
        "gate": gate.upper(),
        "tier3": tier3,
        "committee": com.get("verdict"),
        "vetoed_by": com.get("vetoed_by") or [],
        "stopped_by": stopped_by,
        "campaign_dir": str((d).relative_to(ROOT)),
    }


def paper_for(reg: dict, hypothesis_path: str | None) -> str | None:
    """The paper whose distillation produced this hypothesis, or None.

    None is the ordinary case for every campaign run so far: their hypotheses
    were written by a human and no paper claims them.
    """
    if not hypothesis_path:
        return None
    for pid, row in reg["papers"].items():
        if row.get("hypothesis_path") == hypothesis_path:
            return pid
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--campaign", help="one campaign id")
    g.add_argument("--all", action="store_true",
                   help="every campaign directory that has concluded")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not REGISTRY.exists():
        sys.exit(f"no registry at {REGISTRY}")
    reg = json.loads(REGISTRY.read_text())

    names = ([a.campaign] if a.campaign
             else sorted(p.name for p in CAMPAIGNS.iterdir() if p.is_dir()))
    if a.campaign and not (CAMPAIGNS / a.campaign).is_dir():
        sys.exit(f"no campaign directory {CAMPAIGNS / a.campaign}")

    wrote, unconcluded, unlinked = 0, 0, []
    for name in names:
        v = verdicts_for(name)
        if v is None:
            unconcluded += 1
            continue
        pid = paper_for(reg, v["hypothesis"])
        if pid is None:
            unlinked.append(f"{name} ({v['hypothesis'] or 'no hypothesis'})")
            continue

        row = reg["papers"][pid]
        ids = list(row.get("campaign_ids") or [])
        outs = list(row.get("outcome") or [])
        if name not in ids:
            ids.append(name)
        # Replace this campaign's record rather than appending a duplicate: a
        # re-run of the report stage must be idempotent.
        outs = [o for o in outs if o.get("campaign_id") != name] + [v]
        if not a.dry_run:
            row["campaign_ids"] = ids
            row["outcome"] = sorted(outs, key=lambda o: o["campaign_id"])
        wrote += 1
        print(f"  {pid}  <- {name}")
        print(f"      gate {v['gate']}  tier3 {v['tier3']}  "
              f"committee {v['committee']}"
              + (f" (vetoed by {', '.join(v['vetoed_by'])})" if v["vetoed_by"] else ""))
        stopped = v["stopped_by"] or ("neither the gate nor the committee "
                                      "(validation is not consulted here)")
        print(f"      stopped by: {stopped}")

    print(f"\n  {wrote} outcome(s) recorded, {unconcluded} campaign(s) have not "
          f"concluded, {len(unlinked)} not traceable to a paper")
    if unlinked:
        print("  not traceable (hypothesis written by a human, no paper claims it):")
        for u in unlinked[:12]:
            print(f"    {u}")
    if wrote and not a.dry_run:
        REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
        print(f"  wrote {REGISTRY.relative_to(ROOT)}")
    elif a.dry_run:
        print("  dry run: registry not written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
