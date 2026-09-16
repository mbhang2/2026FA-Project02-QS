# Task 45 — Reading `Gowtam_L1/` before PR #1 merges

**Nothing was moved and nothing was merged.** Everything below was read out of
`origin/Gowtam-L1` with `git show`; the branch was never checked out. This
document is the only write.

> **A note on the number.** Commit `38a1350` on the trunk is also labelled
> "Task 45" — the `_roots.py` fix, committed before this brief arrived. Same
> number, two different pieces of work. This is the one the brief asked for.

**Source under review:** `origin/Gowtam-L1`, tip `e07938c` ("Push Docs",
2026-09-14), 3 commits not contained in `MVP`, 112 files under `Gowtam_L1/`.

---

## The short version

`committee_step.py` is **not tier 3**, and the difference is not a matter of
degree. It is a per-candidate qualitative review panel — three LLM roles reading
one candidate's artifacts and returning a binding `PASS`/`VETO`. It contains **no
trial count, no multiple-testing correction, and no null**. Across the entire
tree the only occurrence of the phrase "multiple testing" is inside a prompt
asking an LLM to mention it in prose.

That is worth stating precisely, because "committee" is the name step 7 uses and
the name collision is doing a lot of work here. This project's gate is a
statement about **N candidates**: the correction is what makes the best of many
trials mean something. Gowtam's committee decides about **one candidate at a
time** and has no representation for the others. It cannot be step 7, not because
it is built badly, but because the quantity step 7 operates on is not present in
it.

And there is a finding here that outranks the taxonomy, in §4 below: **the
holdout is scored on every run and feeds the selection score.** That is not a
subtle leak. It is `run_experiment` simulating the holdout window, `decay_check`
reading its Sharpe, and `composite_score` subtracting a penalty computed from it.

**Merging PR #1 as-is loses nothing.** Details in §6.

---

## 1. What `committee_step.py` actually is

Three new agents (`statistician.md`, `auditor.md`, `judge.md`, all
`edit: deny`), three step modules, three additive schemas. Wired in
`src/loop/run.py::run_full`, per candidate:

```
planner → worker → reviewer → conformance → engine
        → statistician → auditor → committee → package
```

- **statistician** reads `trial_log.json`, returns `{summary, concerns[]}`. Told
  explicitly it "must NOT declare the candidate good or bad".
- **auditor** reads the strategy source and trial log, returns a finding on each
  of four axes — `lookahead`, `bias`, `overfitting`, `assumptions` — each
  `pass | flag | veto`. A lookahead `veto` is a hard blocker.
- **judge/committee** reads both plus the raw metrics and returns
  `{verdict: "PASS" | "VETO", reasoning}`. `judge.md` says: *"There is no human
  gate — your verdict is final."*

The role separation is real and carefully done: the two reviewers cannot write,
only the worker can, and each runs in a fresh session so context does not
accumulate. As a *design for qualitative review* this is good work, and §5 says
what should be kept from it.

But `CommitteeVerdict.verdict` is `Literal["PASS", "VETO"]`, the verdict is
binding, and `PASS` here means **approved**. That is the specific thing the
trunk's tier 3 was built to make impossible.

## 2. Against `audit/`

Two people did build a review tier, independently, and they are not the same
object. The overlap is the word.

| | `audit/` (trunk, tier 3) | `Gowtam_L1` committee |
|---|---|---|
| **unit of decision** | one **campaign** — N candidates scored against one split | one **candidate** |
| **input** | `tier2-summary.json`, a file | six per-candidate JSON artifacts + the strategy source |
| **verdict type** | `Verdict` — `PASS` / `FAIL` / `CANNOT_TELL`, with `assert len(Verdict) == 3` at module level and an assert that no member is named PROMOTE/ACCEPT/APPROVE | `Literal["PASS", "VETO"]` |
| **can it approve?** | **No.** `PASS` is documented as "nothing found to veto. NOT an approval", `as_dict()` emits `can_promote: False`, and `AuditVerdict` has no score, confidence or weight field | **Yes.** `PASS` is the approval, and it is binding with no human stage |
| **"cannot tell"** | a required member; escalates to a human and is recorded as having done so | not representable — the judge must choose |
| **N** | read from the summary, where it means what `runs/ledger.jsonl` means; used to display and sanity-check, never to compute a bar | **absent** |
| **multiple-testing correction** | tier 2 owns it; tier 3 cross-checks the deflation two ways and reports agreement | **absent** |
| **implemented as** | deterministic Python over a JSON file | three LLM invocations |
| **coupling** | imports nothing from `src.core`; nothing imports it | imports the loop, the schemas, the engine, the bridge |

**Where they genuinely differ, and it is not a quality gap.** `audit/` audits a
*campaign's statistics* — did the record get tampered with, do the trials agree
about the code that produced them, does the closed-form deflation agree with the
simulated one. It cannot say anything about whether a particular strategy's code
looks ahead, because it never sees code except as a recorded source blob.
Gowtam's committee audits a *candidate's craft* — reading the actual strategy
file for lookahead, bias, overfitting and contract violations.

Those are complementary questions. The trunk's answer to "does this strategy look
ahead" is structural — the per-bar view raises on future access, and the strategy
runs in a process where the dataset is not importable — so it does not need an
LLM to check. But "structurally prevented" and "empirically verified" are
different claims, and Gowtam's auditor is the only thing in the repo that makes
the second one.

**The asymmetry that decides it.** An auditor that can only *remove* candidates is
safe with a completely uncharacterised error rate; its mistakes cost power and
can never manufacture a discovery. An auditor that can *promote* must have its
error rate measured before anyone acts on it. Nobody has measured an LLM judge's
false-approval rate on this data, and `judge.md` gives it the last word with no
human stage. Adopting its `PASS` as an accept decision would put an unmeasured
classifier in the position the entire project exists to defend.

## 3. What this makes stale

**The brief's premise is half right, and the half that is wrong matters more.**

`docs/design/loop.md:312` and `docs/design/architecture.md:1204` **already say
step 7 exists** and assign it to `audit/` — loop.md even says "which this
document previously recorded as not existing". Those two are current.

`CLAUDE.md:43` still says:

> Not done: validation and holdout are still closed, and the committee tier —
> step 7 of the build plan — does not exist.

So **CLAUDE.md already contradicts loop.md and architecture.md on the trunk,
independently of Gowtam's branch.** PR #1 ships that contradiction today. It is
not caused by `Gowtam_L1/`; Gowtam's work is a second, weaker reason to fix a
line that is already wrong.

**A second staleness found while checking the first.** CLAUDE.md cites
`docs/plans/l1-build-plan.md` twice (lines 23 and 335) and README.md once
(line 37), as the authoritative harness document. **That file is not in the
repository.** It was deleted in `a2e72e7` ("Clean working tree before team-branch
merge experiment"), which is an ancestor of `MVP`. So CLAUDE.md's "step 7 of the
build plan" points at a build plan that no longer exists, and a reader following
either reference gets nothing. No test covers `docs/plans/`, which is why this
survived — `test_architecture_doc.py` asserts architecture.md names every module,
package, source tree and script, but nothing asserts that a prose reference in
CLAUDE.md or README.md resolves to a file that exists.

Both are one-line fixes and neither is this task's to make.

## 4. The five invariants

Checked against the Task 44 brief §5 list. **Four of the five are violated**, and
one of them is the most serious thing in this review.

### 4.1 The split calendar is pre-registered — **violated, twice over**

`splitting/windows.py::split_periods(spec)` *derives* the split from the
`ExperimentSpec` at run time. This is the same defect Task 44 fixed in nalin's
copy, where `windows.py` now reads a committed calendar and checks the spec
against it; `Gowtam_L1` carries the unfixed version.

Worse: `ApprovedCandidate` carries `start`, `train_end`, `val_end`, `end` as
fields, and `run.py` supplies module-level defaults. The split travels **with the
candidate**, which is produced upstream by a proposing agent. A split that
arrives attached to the proposal is not a pre-registration in any sense — it is
the one decision CLAUDE.md lists first under "things not to decide unilaterally",
delegated to the thing being measured.

### 4.2 The holdout — **scored on every run, and inside the selection score**

This is the finding. It is not an inference from structure; it is three
consecutive functions:

```python
# orchestrator/run_experiment.py
val_run  = _run_window(strategy, prices, validation, spec)
hold_run = _run_window(strategy, prices, holdout, spec)      # <- scored
...
decay     = decay_check(val_metrics, hold_metrics)            # <- reads holdout Sharpe
composite = composite_score({**val_metrics, **decay})         # <- penalised by it
```

```python
# metrics/decay.py      "Called automatically on every run — a strategy author never opts in."
decay_flag = hold_sharpe < threshold * val_sharpe            # threshold = 0.5

# metrics/composite.py
score = sharpe - max_drawdown + stability - (1.0 if decay_flag else 0.0)
```

So `composite_score` — the single scalar the committee weighs — is a function of
holdout performance. The holdout metrics are also written into `trial_log.json`,
which is handed as a context file to the statistician, the auditor **and** the
judge. The holdout is read, scored, scored *into the objective*, and shown to
three language models, on every candidate.

In their frame this is deliberate and has a name: a decay-resistance check, and
as a *diagnostic reported once at the end* it is a reasonable idea. But a
quantity that enters the selection score is consumed by selection. Run this over
enough candidates and the holdout has been optimised against — which is the exact
failure this project was built to prevent, arriving through a door marked
"robustness".

The trunk's constraint is unambiguous: *do not open, read or score the holdout*,
and validation and holdout are both still closed.

### 4.3 One ledger, one N — **violated**

`serialization/trial_log.py` writes one `trial_log.json` per candidate. There is
no ledger, no append-only record, no `purpose` field, no N, and no accounting for
crashed or abandoned runs. A candidate that fails sets `state.status = "FAILED"`
in a `state.json` under `candidates/` — **and `candidates/` is gitignored**, so
the record of what was tried is not in the repository at all.

This is a sharper version of what Task 42 found in daksh's tree, where
`n_trials` was at least a file count. Here there is no denominator to be wrong.

### 4.4 One scored engine, one scored metric, one gate — **violated**

`Gowtam_L1` carries a complete second stack: `orchestrator/run_experiment.py`
(the engine), `execution/simulator.py` (fills at the next bar's open, costs in
bps on fractional notional), `execution/portfolio.py` (the equity curve), and
`metrics/{returns,risk,stability,decay,concentration,composite}.py` (nine
metrics plus a composite objective).

Positions are **fractions of capital in −1..1**. The harness trades integer
contracts. As Task 41 already recorded for nalin's engine, a port across that
boundary is **not score-comparable with its source** — the two numbers do not
measure the same strategy.

**Note what changed direction here.** The trunk's `loop/` — descended from
`Gowtam/L1/` — has `evaluate_step.py` and `STRATEGY_FILE = "current.py"`, and it
drives *this* harness by writing the strategy file and invoking `run.py` as a
subprocess. That is precisely why Task 41 kept it: a second independent driver
over one interface is evidence the interface is real.

`Gowtam_L1/` has **no `evaluate_step.py`, and the strings `run.py` and
`current.py` do not appear anywhere in its source.** `STRATEGY_FILE` is
`"train.py"`. The bridge to the harness is gone, replaced by its own engine.
Whatever else this branch is, it is no longer a second driver over our interface —
it forked away from it.

### 4.5 The boundary holds — **violated**

`worker.md` is `permission: {edit: allow, bash: allow}`, and the only thing
confining it is prose: *"Ground rule (non-negotiable): all of your implementation
goes into one single file — `candidates/{id}/train.py`."*

The trunk's boundary is `AGENT_ALLOWLIST = ("src/strategies/",)` enforced four
ways — tool permissions, a CI path check, import guards inside the scoring
modules, and `CHILD_MODULE_ALLOWLIST` in the strategy's own process. CLAUDE.md is
explicit that the last two are the ones that hold under an adversary, "because
configuration drifts and gets copied wrong". A ground rule in a prompt is weaker
than configuration, and `candidates/` is outside the allowlist and outside git.

The `auditor.md` and `statistician.md` roles are `edit: deny` but `bash: allow`,
with the prompt describing the permitted uses as read-only — including `pytest`
and `python -c`. That is an execution channel governed by a description of intent.
`scripts/agent_check.py`'s docstring is the long argument for why that is not
enough; it is worth reading before wiring these roles to anything scored.

### 4.6 Attribution — **fine**

Git carries it, and `Gowtam_L1/` is a separate directory from the `Gowtam/L1/`
that became `loop/`, so nothing on the trunk overwrote anything of Gowtam's.

## 5. Proposed role, and what it would take

Under Task 41's three roles — **driver**, **ported component**, **differential
reference** — this is a **differential reference**, with one component that
deserves to be promoted out of it.

It cannot be a driver: §4.4, the bridge to `run.py` is gone. It cannot be ported
wholesale: §4.2 and §4.1 are not adaptations, they are a different and
incompatible methodology.

But it is a *good* differential reference, and specifically:

**Promote `execution/leakage_guard.py::assert_causal_vectorized`.** This is the
most interesting thing on the branch. It perturbs every bar at or after a cutoff
by a large offset and asserts that positions strictly before the cutoff are
unchanged — an **empirical** test for lookahead that runs once per strategy, O(n).
The harness prevents lookahead *structurally*; this probe would let us
**verify the prevention rather than assert it**, which is CLAUDE.md's "measure it;
do not infer it" applied to the repo's central safety claim. It is also the
natural adversarial test for any vectorised strategy that gets ported in.

**What the differential reference would take**, in rough order of cost:

1. **Land the tree unmodified under `alternatives/`.** It does not belong in
   `alternatives/l1_engine/` — that is nalin's engine, and these are two distinct
   second engines. A sibling (`alternatives/gowtam_committee/` or similar, the
   name is Gowtam's to choose) keeps them separate.
2. **It does not currently run as committed** — see §5.1. That is a prerequisite
   to any of the below and only Gowtam can supply it.
3. **Rename its top-level `src` package.** `pyproject.toml` declares
   `include = ["src*"]` and every module imports `from src.…`. The repo root
   already has `src/core` and `src/strategies`. This is *exactly* the two-`src`
   collision Task 39 found and Task 44 removed, and merging as-is reintroduces
   it. Imports become `alternatives.<name>.…`, mechanically.
4. **Declare `pydantic`.** Nine modules import it; `pyproject.toml` does not list
   it. It is not in this repo's lockfile either, and adding it moves
   `environment_fingerprint` on every committed row — the same costed decision
   nalin's seven skipped tests are parked behind, and the same answer applies:
   skip with a reason naming what they need.
5. **Fix the root walk.** `loop/paths.py` uses
   `Path(__file__).resolve().parent.parent.parent`, which is the pattern
   `tests/test_no_depth_counted_roots.py` now fails the build for. Use the tree's
   `_roots.py`.
6. **Relocate 12 root-level scratch scripts** — `bb_test.py`, `psar_test.py`,
   `test_division.py`, `test_shape.py`, `test_proto.py`, `test_subclass.py`,
   `test_final.py`, `test_orch.py`, `test_pd_mask.py`, `test_indicators*.py`.
   Its own `testpaths = ["tests"]` hides them; under this repo's root suite,
   `test_*.py` at a collected path is collected.
7. **Its 110 tests join the suite**, skipped with reasons where they cannot pass,
   same rule as everyone's. None deleted, none weakened.
8. **Mark it unscored, loudly**, next to `composite_score` in particular. Task 41
   already settled that a composite is "a recorded diagnostic and never the
   objective"; this one additionally reads the holdout, so it needs the stronger
   label.

**Not proposed, and deliberately:** adopting the committee's `PASS` as an accept
decision, in any tier. §2's asymmetry is the reason. If the auditor's
four-axis review is wanted on the scored path, the shape that fits is
veto-or-escalate — findings that can remove a candidate or raise a
`CANNOT_TELL`, never approve one — which is `audit/`'s existing contract and
would let the two tiers compose instead of compete.

**Nothing above was executed.** It is a proposal for Gowtam's PR, not a plan I
have started.

### 5.1 It does not run as committed

Stated plainly because it changes what "review this" can mean, and because it is
very likely just an untracked file on one laptop:

- `src/data/ticks.py` **is not in the repository**. It is imported at module top
  by `orchestrator/run_experiment.py`, `loop/reproducibility.py`,
  `experiments/benchmark_engine.py` and `tests/test_tick_loader.py`. The import
  chain `loop/run.py → packaging/package_candidate.py → orchestrator/run_experiment.py`
  means **`python -m src.loop.run` fails at import** on a clean clone of this
  branch. `.gitignore` does not exclude it, so this looks like a missed `git add`
  rather than a decision.
- `run_train.py` imports from
  `candidates/hypothesis_md_learning_the_dynamics_of_t_20260911_153217_480131/train.py`,
  and `candidates/` is gitignored.

I did not attempt to reconstruct either. Per the standing rule: where something
cannot run, say so and say what it needs, rather than inventing the file.

## 6. Does merging PR #1 lose anything?

**No. Merge it.**

- `Gowtam_L1/` is a **different directory** from the `Gowtam/L1/` that Task 44
  moved into `loop/`. PR #1 does not touch it, does not overwrite it, and does
  not conflict with it. Nothing of Gowtam's is deleted or shadowed by the merge.
- Gowtam's 3 commits branch from `24f8e59` (`main`, 2026-09-08), which is an
  ancestor of `MVP`. After PR #1 merges, his branch still has a clean merge base
  and **merges cleanly afterwards as its own PR** — it adds one new top-level
  directory and touches nothing the trunk owns. The only mechanical collision is
  the top-level `src` package name (§5, item 3), which is a rename inside his own
  tree and is not made harder by PR #1 landing first.
- Sequencing them the other way round would be worse: it would put a tree that
  does not import (§5.1) and that scores the holdout (§4.2) inside the diff that
  is meant to establish the layout.

**One thing should be fixed before PR #1 merges, and it is not Gowtam's.**
CLAUDE.md:43 says the committee tier does not exist while loop.md:312 and
architecture.md:1204 say it does and name `audit/`. That contradiction is on the
trunk now and ships with this PR. The same edit should drop or repoint the three
references to `docs/plans/l1-build-plan.md`, which was deleted in `a2e72e7`
(§3). Two lines and a citation; no code.

---

## What this review did not do

- It did not run anything from `Gowtam_L1/`, which §5.1 explains is not currently
  possible from the committed tree.
- It did not compare the two engines numerically on the same input. That is the
  thing a differential reference is *for*, and it is the natural Task 46 once the
  tree imports — Tasks 41, 42 and 43 each found something real by doing exactly
  that, and this tree has a second fill model, a second metric set and a second
  causality probe to check ours against.
- It did not assess whether the four audit axes are the right four, or whether an
  LLM can judge them reliably. The second question is the one that would have to
  be **measured** before any of this touches a scored decision, and measuring it
  needs labelled candidates nobody has.
