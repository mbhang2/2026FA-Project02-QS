# What a run calls, in order

Which files execute, and in what order, at three levels: a **session**, a
**campaign** inside it, and a **trial** inside that. Indentation is "called by".
`→ writes` names what a step leaves behind. For what each file is, see
[FILES.md](FILES.md); for how each part works, the guides in the
[documentation map](README.md).

## 1. A session

```
scripts/research_session.py --plan config/sessions/<plan>.yaml
│
├─ phase 1: for each campaign in the plan (or family)
│   ├─ scripts/nq_campaign.py --stage register      ── section 2a
│   ├─ scripts/nq_campaign.py --stage run           ── section 2b
│   ├─ scripts/nq_campaign.py --stage nulls         ── section 2c
│   ├─ scripts/nq_campaign.py --stage report        ── section 2d
│   └─ (family, not the last campaign)
│       scripts/refine_hypothesis.py --campaign <k> --commit   ── section 3
│         → the next campaign's question, then back to register
│
├─ phase 2: validation at the stop, every winner in one pass
│   └─ scripts/nq_campaign.py --stage validate      ── section 2e
│
├─ phase 3: scripts/l2_gate.py                       → l2-verdict.json
├─ phase 4: scripts/holdout.py --check               (opened only if armed at launch and L2 passed)
└─ → runs/sessions/<session>.json, logs in runs/sessions/<session>/
```

`scripts/rehearse.py` runs exactly this with `PRACTICUM_RUNS_ROOT=.scratch/rehearsal`
and the synthetic `rehearsal` profile, then checks the outputs.

## 2. A campaign — `scripts/nq_campaign.py`

### 2a. `--stage register`

```
nq_campaign.main
├─ argument refusals (exactly one of --hypothesis / --question; register-only flags)
└─ stage_register
    ├─ config/run.yaml                           the profile, campaign settings
    ├─ seed checks: src/core/loop/campaign.declares_fit, LLMAgent._static_ok
    ├─ src/core/loop/llm.py  LLMAgent.preflight   the proposer serves itself?
    ├─ (only with --question)  scripts/write_hypothesis.py  write_unattended
    │   ├─ scripts/distil_paper.py   campaign_in_flight, _client, lint, length
    │   ├─ scripts/write_hypothesis.py  pick_papers  (data/papers/registry.json)
    │   ├─ audit/committee/llm.py     preflight + the writer's calls
    │   │     config/prompts/question-distil.md
    │   ├─ gates: src/core/loop/hypothesis.py (lint), src/core/research/outcome_lint.py,
    │   │         scripts/grounding.py, sections and length
    │   ├─ scripts/review_hypothesis.py  the review seat  (config/prompts/hypothesis-review.md)
    │   ├─ src/core/loop/fidelity.py  load  (the constraints file)
    │   └─ git add + git commit -- <files>    → config/hypotheses/<slug>.{md,constraints.yaml,origin.json}
    ├─ constraints_for → src/core/loop/fidelity.py load
    ├─ agent_for → src/core/loop/hypothesis.py load, src/core/loop/llm.py LLMAgent.config
    ├─ (fidelity_judge on) scripts/fidelity_judge.py   prompt digest
    ├─ (--generation-width auto) scripts/capacity.py decide
    └─ src/core/loop/campaign.py  CampaignRecord.write    → campaign.json
```

### 2b. `--stage run`

```
stage_run
├─ CampaignRecord.read + hypothesis digest check  (src/core/loop/hypothesis.py)
├─ (width > 1) scripts/capacity.py, scripts/worker_tree.py create  → .scratch/workers/w*
└─ for each generation of width W, each slot in a thread:
    ├─ LLMAgent.propose                           ── section 4
    ├─ src/core/loop/campaign.py  run_trial       ── section 5
    ├─ (width > 1) merge the worker's ledger, sources and fills back
    ├─ src/core/loop/fidelity.py  check_trial     → fidelity.jsonl, fidelity-judge.jsonl
    ├─ drain_log                                  → agent-calls.jsonl
    └─ champion advances on a strict improvement  → champion.py, state.json
```

### 2c. `--stage nulls`

```
stage_nulls
├─ run.py build_dataset  (the campaign's own profile and seed)
├─ src/core/scoring/metrics.py  session_calendar
└─ per scored candidate:
    ├─ scripts/replay.py  load_fill_log
    ├─ src/core/engine/reconstruct.py  reconstruct
    └─ src/core/scoring/randomization/round_turn.py  null_sample   (or random_trader.py)
→ nulls.json
```

### 2d. `--stage report`

```
stage_report
├─ src/core/loop/campaign.py  summarise   (the gate: max_of_n, src/core/scoring/correction.py)
│                                         → result.json, summary.json
├─ scripts/campaign_summary.py            → tier2-summary.json
├─ audit/tier3.py  audit_campaign_summary → tier3-verdict.json
├─ committee_evidence → audit/committee/review.py  review   (four auditors in parallel,
│     audit/committee/llm.py, audit/committee/prompts/*.md)  → committee-verdict.json
└─ _record_outcome (scripts/record_outcome.py)  the verdicts, back onto the paper that
                                          suggested the question, if one did
```

### 2e. `--stage validate`

```
stage_validate
├─ the winner's source from the ledger
├─ validation_profile_for  (<base>-train → <base>-validation in config/run.yaml)
└─ run.py --purpose check --profile <base>-validation      ── section 5
→ validation.json, validation.jsonl, validation-null.json
```

## 3. Between campaigns — `scripts/refine_hypothesis.py`

```
refine_hypothesis.main
├─ validation_readable        refuses if any validation artefact exists
├─ evidence                   campaign k: what the strategy did, concentration,
│                             the auditors (committee-verdict.json); scores withheld
├─ audit/committee/llm.py  preflight
├─ scripts/outcome_retrieval.py  plan
│   ├─ diagnose               up to 3 problems, each quoting the evidence
│   ├─ (--fetch-new) scripts/retrieve.py → fetch_papers, screen_papers, rank_papers
│   ├─ retrieve               BM25 over data/papers/registry.json
│   └─ vet                    keep only papers that answer their problem
├─ context → the refiner's call   config/prompts/hypothesis-refine.md
├─ gates: lint, grounding, family-unchanged, length
├─ scripts/review_hypothesis.py   hypothesis seat + family seat (config/prompts/family-review.md)
├─ refuse if either seat never read it
└─ write + git commit (Machine-Written)    → config/hypotheses/<slug>.md
```

## 4. One proposal — `LLMAgent.propose` in `src/core/loop/llm.py`

```
propose(history)
├─ _pick_parent               the champion, or (explore_epsilon) an earlier source
├─ _prompt                    config/prompts/strategy-agent.md + docs/agent/*.md
│                             + docs/design/context-api.md + hypothesis + history
├─ _one → _one_attempt        HTTP to the gateway; retries 429/5xx
├─ _static_ok                 src/core/checks/ (compile, AST, mypy)   ×2 repairs
├─ _on_question               src/core/loop/fidelity.py code_violations,
│                             src/core/loop/perturb.py; scripts/fidelity_judge.py
└─ _after_static              src/core/loop/probe.py  probe_source   ×2 probe repairs
    └─ run.py under the synthetic probe profile          ── section 5
→ the source for the trial
```

## 5. One trial — `run_trial` in `src/core/loop/campaign.py`

```
run_trial(source)
├─ write src/strategies/current.py
└─ python run.py --profile <profile>
    ├─ config/run.yaml, data/manifest.json
    ├─ src/core/data/<dataset>.py           the split, the holdout lock, sessions
    ├─ src/core/engine/remote/adapter.py    starts the child, sets CPU/memory limits
    │   └─ src/core/engine/remote/child.py  imports current.py; the strategy runs here
    ├─ src/core/engine/engine.py, fills.py, bars.py, context.py   the walk
    ├─ src/core/engine/reconstruct.py       blotter from the fill log
    ├─ src/core/scoring/scorer.py, metrics.py   the score
    ├─ src/core/scoring/integrity.py        fingerprints
    └─ src/core/runlog.py                   one ledger row
→ ledger.jsonl (+1 row), sources/<digest>.py, fills/<digest>.jsonl.gz
```
