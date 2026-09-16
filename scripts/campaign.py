#!/usr/bin/env python3
"""Run a campaign: a pre-registered budget of trials, then the correction.

    python scripts/campaign.py --budget 200 --q 0.05 --seed 1
    python scripts/campaign.py --budget 20 --crash-fraction 0.1   # accounting
    python scripts/campaign.py --summarise runs/campaigns/<id>/     # re-read

**The agent is a stub with no LLM in it**, and that is the point of running this
first. The null generator proposes strategies with no edge by construction, so a
campaign of them has a known answer -- zero real discoveries -- and the
correction can be calibrated against it before any real result depends on it.

The campaign record is written and hashed **before** the first trial. Budget,
stopping rule, q, the correction set and the stub's seed are all in it. It is
re-read and re-hashed at the end, and a mismatch is reported rather than
swallowed: a budget or a q chosen after seeing results is the failure this whole
project exists to prevent.

Training split only. Nothing here reads validation or holdout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.loop.agents import NullGenerator  # noqa: E402
from src.core.loop.campaign import (  # noqa: E402
    CampaignRecord, CampaignResult, new_campaign_id, run_campaign, summarise,
)

CAMPAIGNS = REPO_ROOT / "runs" / "campaigns"


def render(summary: dict) -> str:
    L = []
    c = summary["correction"]
    L.append(f"campaign      {summary['campaign_id']}   record "
             f"{summary['record_digest'][:16]}"
             + ("   TAMPERED" if summary["record_tampered"] else ""))
    L.append(f"budget        {summary['budget']}   q {summary['q']}")
    L.append(f"N (trials)    {summary['n_trials']}"
             f"   harness failures (not trials) {summary['harness_failures']}")
    L.append(f"outcomes      {summary['outcomes']}")
    L.append(f"wall          {summary['wall_clock_s']}s")
    L.append("")
    L.append("PER-TRIAL GATE, uncorrected")
    L.append(f"  accepts at far <= 0.001        "
             f"{summary['per_trial_accepts_at_0.001']}")
    if "uncorrected" in summary:
        u = summary["uncorrected"]
        L.append(f"  best score                     {u['best_score']:+.4f}")
        L.append(f"  its nominal FAR                {u['best_far']:.6g}")
        L.append(f"  candidates at far <= q         {u['n_at_or_below_q']}")
        L.append(f"  {u['reading']}")
    L.append("")
    L.append("CORRECTED")
    L.append(f"  Benjamini-Hochberg (FDR, q={summary['q']})   "
             f"{c['bh_discoveries']} discoveries")
    L.append(f"  Holm (FWER, q={summary['q']})                {c['holm_discoveries']} discoveries")
    if "deflation" in summary:
        d = summary["deflation"]
        L.append(f"  expected max of N under the null    {d['expected_max_under_null']:+.4f}")
        L.append(f"  best score minus that bar           {d['margin']:+.4f}"
                 f"   clears: {d['clears_selection_bar']}")
    L.append("")
    if c["decisions"]:
        L.append("  top of the ordering:")
        L.append(f"    {'rank':>4} {'far':>10} {'BH crit':>10} {'BH':>4} "
                 f"{'Holm crit':>10} {'Holm':>5}  label")
        for d in c["decisions"][:5]:
            L.append(f"    {d['rank']:>4} {d['far']:>10.6g} "
                     f"{d['bh_critical']:>10.6g} {str(d['bh_accepted']):>4} "
                     f"{d['holm_critical']:>10.6g} {str(d['holm_accepted']):>5}  "
                     f"{d['label']}")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget", type=int, default=200,
                    help="trials. Pre-registered: fixed before the campaign runs "
                         "and not revisited because a result looks interesting.")
    ap.add_argument("--q", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=1, help="the stub's seed")
    ap.add_argument("--crash-fraction", type=float, default=0.0,
                    help="fraction of proposals that raise, for verifying that a "
                         "crashed trial still counts toward N")
    ap.add_argument("--profile", default="campaign")
    ap.add_argument("--note", default="")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--summarise", type=Path, default=None,
                    help="re-read a finished campaign directory instead of "
                         "running one")
    args = ap.parse_args(argv)

    if args.summarise:
        summary = json.loads((args.summarise / "summary.json").read_text())
        print(render(summary))
        return 0

    campaign_id = new_campaign_id()
    out = args.out or (CAMPAIGNS / campaign_id)
    out.mkdir(parents=True, exist_ok=True)

    record = CampaignRecord(
        campaign_id=campaign_id,
        profile=args.profile,
        budget=args.budget,
        q=args.q,
        agent="src.core.loop.agents.NullGenerator",
        agent_seed=args.seed,
        agent_crash_fraction=args.crash_fraction,
        dataset="synthetic", split="train",
        note=args.note or ("null campaign: every candidate is edgeless by "
                           "construction, so the known answer is zero real "
                           "discoveries"),
        written_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    agent = NullGenerator(seed=args.seed, crash_fraction=args.crash_fraction)

    print(f"campaign {campaign_id}: {args.budget} trials on the training split, "
          f"q={args.q}")
    print(f"record   {out / 'campaign.json'}", flush=True)
    result = run_campaign(agent, record, ledger=out / "ledger.jsonl",
                          record_path=out / "campaign.json")
    summary = summarise(result, dataset="synthetic")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print()
    print(render(summary))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
