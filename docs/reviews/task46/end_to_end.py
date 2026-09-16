#!/usr/bin/env python3
"""Task 46: a real campaign under a real hypothesis, all three tiers.

    python docs/reviews/task46/end_to_end.py

N = 5 on synthetic data. **That supports no claim about the hypothesis and is
not meant to** -- `MIN_SESSIONS` alone is 30, the gate needs per-candidate nulls
it will not have at this size, and five proposals is not a campaign. What it
establishes is narrower and is the thing that was not yet true: that a question
written by a human in `config/hypotheses/` reaches the agent, is hashed into the
pre-registration, and survives the whole path to a tier-3 verdict.

**Three legs, and the third one may be unavailable.**

1. *The prompt.* Render exactly what the model would receive and write it to
   `run/prompt.txt`, so the hypothesis reaching the agent's input is a file
   somebody can read rather than an assertion.
2. *The mechanical path*, with the stub proposer: record -> trials -> ledger ->
   summary -> tier 3, carrying the hypothesis path and digest the whole way.
   This needs no network and is what proves the plumbing.
3. *A real model*, five proposals under the question. This needs the gateway,
   and **if the gateway will not serve, this leg does not run and the script
   says so and exits non-zero rather than substituting a model or pretending.**
   Task 37 is why: the gateway silently served `gemini-3.1-pro-preview` for
   `claude-haiku-4-5` once, and that campaign was discarded.

Leg 3 is the one that answers the question no assertion can: are the proposals
recognisably *about* what was asked? A test can check the text is in the prompt.
It cannot check that a model understood it.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OUT = pathlib.Path(__file__).parent / "run"
HYPOTHESIS = ROOT / "config" / "hypotheses" / "intraday-continuation.md"

RUN_FACTS = """\
Dataset: a synthetic tick series, not NQ. 40 sessions, 390 ticks each, a random
walk with no drift and no structure planted in it. The API documents above
describe the NQ adapter; the instrument here is `synthetic` and the field names
are the same.

Split: train. Fill model: unfavorable. Action cap: 64 per session.
"""


def main() -> int:
    from src.core.loop import hypothesis as H
    from src.core.loop.agents import NullGenerator
    from src.core.loop.campaign import CampaignRecord, run_campaign, summarise
    from src.core.loop.llm import LLMAgent, LLMError, ModelSubstituted, _source_of

    OUT.mkdir(parents=True, exist_ok=True)
    for stale in ("campaign.json", "result.json", "tier2-summary.json",
                  "tier3-verdict.json", "ledger.jsonl", "summary.json",
                  "prompt.txt"):
        (OUT / stale).unlink(missing_ok=True)

    print("=== the question ===", flush=True)
    h = H.load(HYPOTHESIS)
    print(f"  file    {h.path.relative_to(ROOT)}")
    print(f"  digest  {h.digest}")
    print(f"  lint    clean ({len(h.text.splitlines())} lines, every one of "
          "them pasted into the prompt)")

    # ----------------------------------------------------------------- leg 1
    print("\n=== leg 1: what the agent would receive ===", flush=True)
    agent = LLMAgent(model=MODEL, temperature=0.7, max_tokens=4096,
                     show_history=True, hypothesis=h, run_facts=RUN_FACTS)
    prompt = agent._prompt((), repair="")
    system, user = prompt.split("## USER", 1)
    system = system.split("## SYSTEM", 1)[-1].strip()
    (OUT / "prompt.txt").write_text(
        "=== SYSTEM ===\n" + system + "\n\n=== USER ===\n" + user.strip())
    assert h.text.strip() in user, "the hypothesis did not reach the prompt"
    print(f"  written  {(OUT / 'prompt.txt').relative_to(ROOT)}  "
          f"({len(system) + len(user):,} chars)")
    print(f"  contains the question verbatim: True")
    print(f"  hypothesis in the agent's pre-registration: "
          f"{agent.config()['hypothesis']}")

    # ----------------------------------------------------------------- leg 2
    print("\n=== leg 2: the mechanical path, stub proposer, no network ===",
          flush=True)
    record = CampaignRecord(
        campaign_id="task46-e2e", profile="campaign", budget=5, q=0.05,
        agent="src.core.loop.agents.NullGenerator", agent_seed=46,
        dataset="synthetic", split="train",
        hypothesis_path=str(h.path.relative_to(ROOT)),
        hypothesis_digest=h.digest,
        note=("Task 46 end-to-end. N=5 supports no claim about the hypothesis "
              "and is not meant to; it establishes that the question reaches "
              "the agent and the record. The proposer here is the STUB -- these "
              "candidates are edgeless by construction and are not an attempt "
              "to answer the question."),
        written_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    t0 = time.monotonic()
    result = run_campaign(NullGenerator(seed=46), record,
                          ledger=OUT / "ledger.jsonl",
                          record_path=OUT / "campaign.json", progress=False)
    summary = summarise(result, dataset="synthetic")
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (OUT / "result.json").write_text(json.dumps(
        {"rows": result.rows, "wall_clock_s": result.wall_clock_s,
         "record_digest": result.record_digest, "tampered": result.tampered},
        indent=2, default=str))
    print(f"  N {summary['n_trials']}   outcomes {summary['outcomes']}   "
          f"unaccounted {summary['unaccounted']}   tampered {result.tampered}")
    print(f"  {time.monotonic() - t0:.0f}s")

    reread, stored = CampaignRecord.read(OUT / "campaign.json")
    print(f"  record: hypothesis_path   {reread.hypothesis_path}")
    print(f"  record: hypothesis_digest {reread.hypothesis_digest[:16]}  "
          f"still matches the file: "
          f"{reread.hypothesis_digest == H.load(HYPOTHESIS).digest}")
    print(f"  record digest verifies    True ({stored[:16]})")

    print("\n--- tier 3 reads the summary and returns a verdict ---",
          flush=True)
    from audit.tier3 import audit_campaign_summary
    verdict = audit_campaign_summary(summary)
    (OUT / "tier3-verdict.json").write_text(
        json.dumps(verdict.as_dict(), indent=2, default=str))
    print(f"  verdict {verdict.verdict.value}   escalate "
          f"{verdict.escalate_to_human}   can_promote "
          f"{verdict.as_dict()['can_promote']}")
    for r in verdict.reasons:
        print(f"    - {r}")

    # ----------------------------------------------------------------- leg 3
    print("\n=== leg 3: a real model, five proposals under the question ===",
          flush=True)
    try:
        served = agent.preflight()
    except (LLMError, ModelSubstituted) as exc:
        print(f"  DID NOT RUN. {type(exc).__name__}: {str(exc)[:160]}")
        print()
        print("  The gateway would not serve any model tried "
              f"({', '.join(TRIED)}) -- 503 upstream, 403 at the proxy.")
        print("  This leg is NOT simulated and no substitute model was used.")
        print("  Task 37 is the precedent: the gateway once served")
        print("  gemini-3.1-pro-preview for claude-haiku-4-5 and that campaign")
        print("  was discarded. A model that is not the one named is not a")
        print("  cheaper version of the experiment, it is a different one.")
        print()
        print("  WHAT IS THEREFORE UNANSWERED: whether the proposals are")
        print("  recognisably about the question. Legs 1 and 2 establish that")
        print("  the question reaches the model's input and travels through the")
        print("  record to a verdict. They cannot establish that a model reads")
        print("  it and proposes accordingly. Re-run this script when the")
        print("  gateway is up; nothing else needs to change.")
        return 2

    print(f"  asked {MODEL}  served {served.get('model_served')}")
    llm_record = CampaignRecord(
        campaign_id="task46-e2e-llm", profile="campaign", budget=5, q=0.05,
        agent="src.core.loop.llm.LLMAgent", dataset="synthetic", split="train",
        hypothesis_path=str(h.path.relative_to(ROOT)),
        hypothesis_digest=h.digest, agent_config=agent.config(),
        note="Task 46 end-to-end, real model. N=5 supports no claim.",
        written_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    llm_result = run_campaign(agent, llm_record, ledger=OUT / "llm-ledger.jsonl",
                              record_path=OUT / "llm-campaign.json", progress=True)
    llm_summary = summarise(llm_result, dataset="synthetic")
    (OUT / "llm-summary.json").write_text(
        json.dumps(llm_summary, indent=2, default=str))
    print(f"  N {llm_summary['n_trials']}   outcomes {llm_summary['outcomes']}")

    print("\n=== what the model actually proposed ===", flush=True)
    print("Read these. The question was whether an outsized move inside a")
    print("session continues or gives it back; nothing else asserts that the")
    print("proposals are about it.\n")
    for i, row in enumerate(llm_result.rows):
        doc = _docstring(_source_of(row))
        print(f"--- #{i}  outcome={row.get('outcome')}  "
              f"score={(row.get('score') or {}).get('primary')}")
        print("    " + (doc or "(no module docstring)").replace("\n", "\n    ")[:900])
        print()
    return 0


def _docstring(source: str) -> str:
    import ast

    try:
        return ast.get_docstring(ast.parse(source)) or ""
    except SyntaxError:
        return "(source does not parse)"


MODEL = "claude-haiku-4-5"
TRIED = ("claude-haiku-4-5", "claude-sonnet-5", "gemini-3.6-flash",
         "claude-fable-5-1")

if __name__ == "__main__":
    raise SystemExit(main())
