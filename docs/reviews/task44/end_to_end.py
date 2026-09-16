#!/usr/bin/env python3
"""One short run through all four tiers, after Task 44's restructure.

    python docs/reviews/task44/end_to_end.py

"It imports" is not "it runs". This drives a real N = 5 campaign on synthetic
training data with the stub proposer, scores it through the gate, writes the
tier-2 summary, and hands that to tier 3 -- exercising every seam the
restructure touched.

The stub proposer rather than the LLM on purpose: the endpoint's availability is
not what this is testing, and `NullGenerator` makes the campaign deterministic.
N = 5 is far below anything that could support a claim, which is why this writes
to a scratch directory and no ledger row from it counts toward anything.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
OUT = REPO / "runs" / "tmp-task44-e2e"


def main() -> int:
    import datetime as dt

    from src.core.loop.agents import NullGenerator
    from src.core.loop.campaign import (CampaignRecord, run_campaign, summarise)

    OUT.mkdir(parents=True, exist_ok=True)
    for stale in ("campaign.json", "result.json", "tier2-summary.json",
                  "tier3-verdict.json", "ledger.jsonl"):
        (OUT / stale).unlink(missing_ok=True)

    print("=== tier 1+2: a campaign of 5 on the training split ===", flush=True)
    record = CampaignRecord(
        campaign_id="task44-e2e", profile="campaign", budget=5, q=0.05,
        agent="src.core.loop.agents.NullGenerator", agent_seed=44,
        dataset="synthetic", split="train",
        note="Task 44 end-to-end check. N=5 supports no claim and is not meant to.",
        written_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    t0 = time.monotonic()
    result = run_campaign(NullGenerator(seed=44), record,
                          ledger=OUT / "ledger.jsonl",
                          record_path=OUT / "campaign.json", progress=False)
    summary = summarise(result, dataset="synthetic")
    (OUT / "result.json").write_text(json.dumps(
        {"rows": result.rows, "wall_clock_s": result.wall_clock_s,
         "record_digest": result.record_digest, "tampered": result.tampered},
        default=str))
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"  N {summary['n_trials']}   outcomes {summary['outcomes']}   "
          f"unaccounted {summary['unaccounted']}")
    print(f"  gate computed: {summary['gate']['is_the_gate']}  "
          f"(no per-candidate nulls at this size, by design)")
    print(f"  {time.monotonic() - t0:.0f}s", flush=True)

    print("\n=== the driver: loop/ writes the file and invokes run.py ===",
          flush=True)
    from loop import evaluate_step, paths as lpaths

    built = lpaths.candidate_file("task44-e2e", lpaths.STRATEGY_FILE)
    built.write_text(NullGenerator(seed=99).propose([]))
    try:
        row = evaluate_step.run_evaluate_step(
            "task44-e2e", ledger=OUT / "driver-ledger.jsonl")
    finally:
        built.unlink(missing_ok=True)
    print(f"  outcome {row['outcome']}   score "
          f"{(row.get('score') or {}).get('primary')}")
    print(f"  verifiable: code_fingerprint {bool(row.get('code_fingerprint'))}  "
          f"artifacts {bool(row.get('artifacts'))}  "
          f"isolation {row.get('isolation', {}).get('level')}  "
          f"reconciled {row.get('reconciliation', {}).get('agree')}")

    print("\n=== tier 2 artifact: the campaign summary ===", flush=True)
    r = subprocess.run([sys.executable, "scripts/campaign_summary.py", str(OUT)],
                       cwd=str(REPO), capture_output=True, text=True)
    print("  " + "\n  ".join(r.stdout.strip().splitlines()[-4:]))
    if r.returncode:
        print(r.stderr[-800:]); return 1

    print("\n=== tier 3: the audit ===", flush=True)
    r = subprocess.run([sys.executable, "-m", "audit.tier3", str(OUT)],
                       cwd=str(REPO), capture_output=True, text=True)
    print("  " + "\n  ".join(r.stdout.strip().splitlines()))
    if r.returncode:
        print(r.stderr[-800:]); return 1

    verdict = json.loads((OUT / "tier3-verdict.json").read_text())
    print(f"\n=== what came out ===")
    print(f"  tier 2 verdict : {verdict['tier2_gate_verdict']}")
    print(f"  tier 3 verdict : {verdict['verdict']}"
          f"   veto: {verdict['is_veto']}   can_promote: {verdict['can_promote']}")
    print(f"  escalated      : {verdict['escalate_to_human']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
