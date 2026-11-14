# Sessions

An overnight run: several campaigns, then the accounting that turns them into
one claim or, far more often, into nothing. `scripts/research_session.py` runs a
plan; `scripts/rehearse.py` runs the same thing on synthetic data.

[Documentation map](../README.md) · rules: pre-registration §4-8, Amendments 10-13 in [design/pre-registration.md](../design/pre-registration.md)

## Files

| File | Role |
|---|---|
| `scripts/research_session.py` | Runs a plan's campaigns, then validation, L2 and the holdout check |
| `config/sessions/*.yaml` | Plans |
| `scripts/l2_gate.py` | The L2 gate: each winner's validation null, corrected over M campaigns |
| `scripts/holdout.py` | The single look at the holdout split: 73 sessions, once, ever |
| `scripts/rehearse.py` | A whole session on synthetic data, then checks that every stage really ran |
| `runs/sessions/<session>.json` | The session record: what ran, what was refined, the L2 result |

## Two shapes of plan

```yaml
# a list: K questions written in advance
session: s-list
campaigns:
  - name: nq-a-01
    hypothesis: config/hypotheses/flow-continuation-5m.md
  - name: nq-a-02
    question: "CVD divergence marks intraday trend exhaustion"   # Amendment 15

# a family: one seed, refined between campaigns
session: s-family
family:
  seed_question: "I want a trend reversal strategy that looks into CVD"  # or seed: <file>
  iterations: 4
  n: 100
  generation_width: 4
  model: gemini-3.1-pro-preview           # proposer
  writer_model: gemini-3.1-pro-preview    # writes question 1 from the statement
  reviewer_model: gemini-2.5-pro          # must clear question 1
  refine_model: gemini-3.1-pro-preview    # writes questions 2..k
  refine_reviewer_model: gemini-2.5-pro   # both refinement seats
  audit_model: gemini-2.5-pro             # the committee
  auditor_models: {leakage: mimo-v2.6-pro} # optional: one auditor on its own model
  judge_model: gemini-2.5-pro             # the fidelity judge
  picker_model: gemini-2.5-pro            # papers offered to the question writer
  retrieval_model: gemini-3.1-pro-preview # diagnosis and vetting between campaigns
  paper_model: gemini-2.5-pro             # screening/ranking, with fetch_new_papers
  fetch_new_papers: false                 # search arXiv between campaigns
  refine: true                            # false = control arm: one question, rerun
```

## The four phases

1. **Campaigns.** Each one: register, run, nulls, report. In a family, the
   refinement after campaign k writes campaign k+1's question (see
   [hypotheses.md](hypotheses.md)). **No validation exists during this phase**,
   which is what makes refining from train results legal.
2. **Validation at the stop.** Every campaign's winner, once, in one pass, on the
   validation profile paired with its training profile (`<base>-train` validates
   on `<base>-validation`).
3. **L2.** `l2_gate.py` corrects over the M campaigns (`max_of_m`, on the
   standardised statistic, Amendment 10). Dropping campaigns that did badly
   would shrink M, so the family is the declared plan, not a choice.
4. **Holdout.** Checked by default. Opened only if the session was armed at
   launch with `--holdout-if-l2-passes "<reason>"` and L2 passed. Once, ever.

**Stopping.** The first Ctrl-C (or `kill -INT -<pid>`) means "stop proposing, go
to the stop": no new campaign starts, and validation still runs. A second one
aborts. A human may stop for any reason that is not a validation number, and
none exists yet. **A failed refinement stops the session without validating**, so a
broken night does not spend validation looks. A failed run, nulls or report
stage marks that campaign (`"<stage> failed"` in the session record) and the
session moves on; in a family, the campaigns after it then have no question.

Other flags: `--dry-run` (prints the plan, runs nothing), `--resume` (skips
campaigns with a ledger, still runs their refinements), `--train-only` (phase 1
only).

## Rehearsals

```bash
python scripts/rehearse.py --campaigns 5 --n 4 --width 2 --keep \
  --seed-question "I want a trend reversal strategy that looks into CVD" \
  --model gemini-3.1-pro-preview --writer-model gemini-3.1-pro-preview \
  --reviewer-model gemini-2.5-pro --refine-model gemini-3.1-pro-preview \
  --refine-reviewer-model gemini-2.5-pro --audit-model gemini-2.5-pro
```

Synthetic data, output under `.scratch/rehearsal/`, every row `purpose: check`:
**not a look** (Amendment 13). It checks that every campaign registered, ran its
budget, built nulls, got a gate verdict and a committee that ran, that the first
question came from the statement, that each refinement reached the next
registration, and that L2 produced a verdict. It skips git commits, so the
commit path is the one thing it cannot test.

## When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `no validation profile 'X-validation'` | The training profile has no validation pair in `config/run.yaml` | Add the pair (`rehearsal-validation` was added 2026-11-12) |
| L2 `VERDICT WITHHELD … campaigns have no validation null` | Those campaigns' winners never traded on validation, so there is nothing to build a null from | A result, not a fault: M cannot be established, so no correction is computed |
| `not scored on validation, so cannot be in a family` | Validation did not run for those campaigns | The validate logs under `runs/sessions/<session>/` |
| `refinement FAILED … STOPPING WITHOUT VALIDATING` | The refiner could not write the next question | Its log under `runs/sessions/<session>/`; often a seat that could not run |
| `a family needs exactly one of seed: … or seed_question:` | Both or neither given | Give one |
| A resumed family skipped a refinement | Should not happen since 2026-10-04 | The session record's `refinements` list |
