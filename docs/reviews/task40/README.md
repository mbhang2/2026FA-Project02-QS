# Task 40 — integrating the teammate trees. What moved, what did not, what broke.

Commit range **`8156af1..97e53cc`**, seven commits on `merge-test-michael`, one
logical change each, every one verified before the next.

The verification, run after every commit:

```
git ls-files -s src/core run.py config data | diff ~/practicum-baseline/tree.txt -
python -m pytest tests/ -q
python scripts/ledger_check.py
```

The tree diff was **empty at every commit**. Invariant 3 holds mechanically, not
by inspection.

---

## 1. What moved, and what was deleted

**Nothing was deleted.** 54 renames, 4 new files, no removals.

| from | to | why |
|---|---|---|
| `nalin/l1-loop/src/` | `nalin/l1-loop/nalin_l1/` | two top-level packages named `src` (§3.1) |
| `nalin/l1-loop/Data/` | `nalin/l1-loop/market_data/` | case collision with root `data/` |
| `nalin/l1-loop/hypotheses/` | `research/hypotheses/` | research input, not harness code |
| `nalin/l1-loop/experiments/dummy_run.py` | `research/experiments/` | same |

New: `nalin/l1-loop/calendars/{placeholder,es-20250703}.yaml`,
`src/strategies/ported_{buy_and_hold,momentum}.py`,
`tests/test_ported_strategies.py`, `research/README.md`.

**`Gowtam/L1/` did not move at all**, beyond a two-line fix to its `paths.py`.
Nothing imports it, it has no entry point, and it is one side of an unresolved
fork; relocating it would have been a vote.

## 2. What I picked between, and what I left

**Picked — the split, and the trial log.** Both were forced by invariants, not
chosen.

**Left standing, deliberately:**

| two of | why not picked |
|---|---|
| **the loops** (`Gowtam/L1/loop/` vs `nalin_l1/loop/`) | The same program, diverged 4–52 lines per module, with byte-identical agent definitions. Picking decides whose loop the team runs — a teammate would reasonably disagree, and neither is the harness's loop. |
| **the fill models** | nalin fills at the next bar's open with bps fees; the harness fills a resting limit only on a trade strictly through it. These are two modelling choices with different optimism, and Task 4's argument is about which one the loop *scores* on. Adopting nalin's would need its own calibration. |
| **the metric sets** | `composite_score` is a second objective function. Which objective the project has is not a filing decision. |
| **the duplicated `.opencode/agent/*.md`** | Three files, byte-identical across both trees. Deduplicating means choosing which tree owns them, which is the loop question again. |

## 3. Where an invariant forced a choice

### 3.1 Invariant 5 — one importable `src` (commit 1)

`nalin/l1-loop/src/` and the root `src/` were both regular packages with the same
top-level name, so `import src` resolved by `sys.path` order:

```
nalin's path first:   src -> nalin/.../src/__init__.py,  src.core -> src/core   (!)
root's path first:    src -> src/__init__.py,            src.loop -> ImportError
```

The first is a package with two parents, and it held together only because the
editable install's `__editable___l1_harness_0_1_0_finder` maps the submodule
explicitly. On a fresh checkout without `pip install -e .`, `src.core`
disappears the moment nalin's directory precedes the root — and `test_layout.py`
and `test_boundary.py` both reason about the identity of the `src` package, so
this was a live threat to invariant 4.

**The alternative** was renaming the *harness's* `src/` instead, which touches
every fingerprinted path in the repo. Not close.

### 3.2 Invariant 1 — the split is read, never derived (commit 4)

`splitting/windows.py` cut three contiguous windows out of `spec.start`,
`spec.train_end`, `spec.val_end` and `spec.end` at run time, arguing in its
docstring that this was a feature: "the holdout exists on *every* experiment by
construction, because it comes from the spec."

Now `split_periods` reads a committed calendar and **refuses** when there is
none. Two registries, searched in order: `config/calendars/` then
`nalin/l1-loop/calendars/`.

**The second registry is where invariant 1 and invariant 3 collided, and it is
the most interesting decision in this task.** Honouring invariant 1 means nalin's
datasets need committed calendars. `config/calendars/*.yaml` is inside
`ARTIFACT_GLOBS`, so adding a file there changes `artifact_digests()` and
re-dates the artifact provenance on **every committed ledger row**. nalin's two
datasets are 240 rows of generated placeholder and one session of ES.

- *The alternative I rejected:* put them in `config/calendars/` and accept the
  fingerprint change. That trades the provenance of every published result for
  the convenience of two fixtures.
- *The alternative I also rejected:* leave the derived split in place for nalin's
  own data. That is choosing against invariant 1.
- *What I did:* a second registry inside nalin's tree, same
  commit-date-is-the-registration rule, and **both calendar files say in their
  header that a real dataset's calendar belongs in `config/calendars/` and that
  moving it there is a human decision with that cost attached.**

**A second thing fell out of it: the embargo was zero.** The derived split had
validation beginning on the day training ended — adjacent observations across a
boundary, which the harness's `Calendar.__post_init__` refuses outright. The
committed calendars put seven days (placeholder) and twenty minutes (ES) between
windows, and `registered_split` raises on a calendar whose windows touch.

`test_splitting.py` was rewritten, because its assertion was
`train.end == validation.start` — a test pinning exactly the defect being
removed.

### 3.3 Invariant 2 — one trial log (commit 5)

`write_trial_log` writes `candidates/<id>/trial_log.json` per candidate. Nothing
counts them today, but the name says "trial log" and the first thing anyone does
with a directory of these is count them.

**I did not port it onto `runs/ledger.jsonl`, and that was the choice.** A ledger
row asserts a `RunTicket` minted by `LedgerRun`, a `code_fingerprint`, artifact
digests, a dataset digest, an isolation level and a reconstruction that
reconciles with the accumulator. nalin's engine runs a strategy in process
against a pandas frame and goes nowhere near `run.py`. Emitting rows from it
would grow N by rows `ledger_check` calls unverifiable — **a quietly inflated
denominator, which is worse than one that is honestly incomplete.**

*The alternative* — porting the logging without porting the run — is precisely
how a denominator gets inflated, so the brief's other option was taken: the loop
stays unintegrated and every record now carries a `ledger` block saying
`is_trial_ledger_row: false`, `counted_toward_n: false`, where the real ledger
is, and why.

---

## 4. The port — what was awkward, what needed a workaround, what was impossible

Two strategies ported from `generate_positions(DataFrame) -> Series` to
`setup`/`on_bars`/`on_fill`/`on_lifecycle`. Notes live in each file's docstring;
the findings are here.

### Impossible: fractional sizing

The source's positions are **fractions of capital** in [-1, 1]. This engine
trades **integer contracts**. `+1.0` becomes `size=1`, which is one contract and
not "fully invested", and there is no expression in the API for the latter.

A scale-free strategy ported to a contract-count engine is a different strategy.
This is not an API gap that more surface would close — it is a different
execution model — but it is the one thing the port could not express, and any
comparison of a ported strategy's score against its source's is invalid because
of it.

### The bug a mechanical port would have had

The source shifts every signal by one bar and says why: *"All signals are shifted
by 1 bar to prevent lookahead bias."* That shift exists because a pandas frame
hands a strategy the bar it is deciding on, so the defence must be written by
hand, by every author, every time.

`ctx.history()` contains only closed bars, and `history[len(history)]` raises
`LookaheadError`. **The shift is already in the type.** Porting it literally
would lag every decision by two bars and silently produce a different strategy.

*The shape does the work the discipline used to do.* This is the strongest thing
the port has to say about the API, and it is a point in the API's favour.
`test_the_port_does_not_double_shift` is the check.

### Awkward, but no workaround needed

- **Positions become order deltas**, so "holding" is the absence of an order and
  has to be written as a guard (`if ctx.position: return`) rather than being the
  default. `pd.Series(1.0, index=...)` says it in one line; here it is a
  negative.
- **Rolling statistics are recomputed, not rolled.** EMA is kept incrementally on
  `self` — it is a recurrence, so this is exact and cheaper, and a test checks
  the alpha against pandas' `adjust=False`. The standard deviation is recomputed
  from `history.last(30)` each bar. The window is bounded at 512, so a strategy
  wanting a 600-bar statistic must keep it itself. The API says so; it is still a
  constraint the source did not have.
- **The state machine ported cleanly and is *easier* here.** `bars_since_entry`
  and the 15-bar minimum hold were loop variables over a whole series; as
  instance attributes they are the natural thing, because "bars since entry" is
  an event rather than an index.

### `ported_momentum` takes no trades on synthetic data, and that is the data

Five sigma of a 30-bar rolling deviation is not reached by Gaussian ticks
aggregated to 5m bars. Run against the ES 1-minute day the source was written
for, the **original** holds a position on 193 of 1,155 bars — so the threshold is
reachable where it was designed to be and inert where it is not.

**The multiplier was not lowered to make it trade.** The shipped default is the
source's 5.0; the decision-logic tests vary it as a test parameter instead.

---

## 5. Stop-and-report items — not decided

### 5.1 nalin's 7 error-ing test files need `pydantic`, and installing it drifts every ledger row

Seven of nalin's 17 test modules fail collection with `ModuleNotFoundError: No
module named 'pydantic'` — an undeclared dependency, true before this task and
unchanged by it. Fixing it means installing pydantic, which changes
`environment_fingerprint` (a hash over the resolved installed environment,
recorded on every ledger row), and adding it to `requirements.lock` changes
`lockfile_digest` (likewise).

Invariant 3 names `src/core/**`, `run.py`, `config/**` and `data/**`, so it does
not literally cover `requirements.lock` — but the consequence is the same class:
`ledger_check` currently reports **environment-drifted: 0**, and it would not
afterwards. Not done. A human decides whether nalin's tests running is worth
re-dating the environment on every committed row.

### 5.2 A real dataset's calendar belongs in `config/calendars/`

§3.2. The second registry is right for fixtures and wrong for anything real.
Moving a calendar into the harness's registry changes `artifact_digests()`; that
is the cost, and it is a human's to accept.

### 5.3 `download_r2.py` touches credentials and was not moved

`nalin_l1/data/download_r2.py` reads `nalin/l1-loop/secrets/r2.env` via a
three-`parent` walk and uses `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ENDPOINT` through boto3. It prints
guidance and **never values** — checked line by line. Left exactly where it is:
the `src` → `nalin_l1` rename preserved its depth, so the walk still lands on the
same `secrets/`. **If it is ever moved, that walk moves with it and will point
somewhere else silently.** `secrets/` is gitignored in both the root and nalin's
own `.gitignore`.

### 5.4 `nalin/l1-loop/calendars/es-20250703.yaml` is not a research split

Three windows carved out of a single session share their regime, their
volatility and their order flow. It is committed so the smoke script has a split
it did not invent at run time, and the file says so in its own header. It should
not be cited as evidence of anything.

---

## 6. Two things found that were not asked about

**Both `Docs/` references were dangling, and always had been.** Gowtam's
`Docs/References/context-api.md` and nalin's `Docs/reports/L1_READY.md` appear in
no branch of this repository. `invoke_opencode` passes a missing `-f` path to
OpenCode without complaint, so **both planners have been running with less
context than their code says they have, silently, since they were written** — on
top of the case problem, where `Docs` resolves on macOS and not on Linux.

Gowtam's is re-aimed at `docs/design/context-api.md`, which exists and is exactly
what the constant name asks for. nalin's is case-corrected and **deliberately not
re-aimed**: guessing which document its planner was meant to read is not a filing
decision. `paths.context_files()` in both trees now drops missing paths and
writes them to stderr, so the next dangling reference announces itself.

**`PROJECT_ROOT` in `Gowtam/L1/loop/paths.py` is `Gowtam/`, not `Gowtam/L1/`.**
Three `parent`s from a file two levels down. `candidate_dir` therefore writes to
`Gowtam/candidates/<id>`. Left as written — changing it would move where that
loop keeps its state — and flagged in a comment.

---

## 7. Final verification

```
tree diff vs baseline        EMPTY                     (at all seven commits)
pytest tests/ -q             589 passed, 24 skipped
pytest nalin/l1-loop/tests   50 passed, 7 errors
scripts/ledger_check.py      N=0 trials, 0 superseded, 0 environment-drifted
```

**The root test count moved from 580 to 589, deliberately.** Nine tests were
added in `tests/test_ported_strategies.py`; no existing test was changed or
removed. nalin's suite went 45 → 50 passed with the same 7 pydantic errors: +4
splitting tests, +1 ledger-block test, and no regressions from the calendar
change.

`test_architecture_doc` caught the two new strategy modules before I did, which
is the check doing its job; the document now names them and says they are
apparatus rather than proposals.
