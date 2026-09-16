# Task 41 — the six overlaps, and what the differential comparisons found

Commit range **`d53c45e..HEAD`** on `merge-test-michael`. Nothing was discarded.

The durable statement of the six roles lives in `docs/design/loop.md`; this
document is the evidence, the disagreements, and the two places the brief's
premises were wrong.

---

## 1. The role of each

| overlap | role | settled by |
|---|---|---|
| the split | committed calendar only, fixture registry **allowlisted** | consequence |
| the trial log | one ledger; verifiable rows or no rows | consequence |
| the loops | `src/core/loop/` drives scored; Gowtam's kept as a second **driver**; nalin's drives its own engine | decided here |
| the `Strategy` contracts | event-driven is the scored contract; nalin's is a different research object, its strategies **ported** | decided here |
| the fill models | **differential reference** | decided here |
| the metric sets | **differential reference**; `composite_score` a recorded diagnostic | decided here |

---

## 2. Every disagreement the comparisons found

### 2.1 `max_drawdown` — opposite sign conventions, and nobody had said so

**The disagreement.** nalin's returns a **positive magnitude** and documents it
(`nalin_l1/metrics/risk.py`: *"returned as a positive magnitude"*). The
harness's returns a **negative** number — `min(worst, cum - peak)` — and its
docstring, *"On the cumulative return path, in return units. Reported, never
scored."*, does not mention the sign at all.

| returns | nalin | harness |
|---|---:|---:|
| mixed | +0.025876 | −0.026000 |
| small | +0.030000 | −0.030000 |
| wide | +0.041344 | −0.040000 |

**Which side I believe: neither, on sign.** It is a convention and both are
internally consistent. The harness's is the one every number in
`docs/design/metric.md` was computed with, so it stays. What was actually wrong
is that nothing anywhere recorded that they differ — and a drawdown comparison
read backwards is the cheapest possible way to conclude the wrong thing about a
candidate. The test now pins both conventions, so a future change to either
fails rather than skewing a comparison silently.

**What I did not do:** add "negative" to the harness's docstring. That is an edit
to `src/core/**`. Reported, not slipped in.

**On the arithmetic they agree to within 0.5%**, which is the real value here:
two independent implementations by different authors, on code nobody had
cross-checked. The residual is compounding versus adding — nalin walks a
multiplicative equity curve, the harness an additive cumulative return path,
which is correct for per-session returns already normalised by notional.

### 2.2 Sharpe — differs by exactly √252, and that is deliberate on both sides

nalin annualises by √252; the harness does not annualise at all. The ratio is
exact to 1e-9 across all three return series.

**Both are right for their purpose, and the harness's reason is the load-bearing
one:** Sortino on per-session returns is compared against an empirical null built
from the same unannualised statistic. A constant factor cancels on both sides
while making the number look like an annual rate it is not — and a number that
looks like an annual rate is a number somebody will quote as one.

The test asserts the ratio **exactly**, so if either side redefines its Sharpe
the failure names it.

### 2.3 Sample standard deviation — agreement to 1e-12

Underneath both Sharpes. `harness_metrics._sd` against `pandas.Series.std`
(ddof=1), three series, agreement at 1e-12. Not a disagreement; recorded because
an independent check of arithmetic nobody had verified is exactly what a
differential reference is for, and a clean result is a result.

### 2.4 The fill models — the difference is the model, and its direction matters

nalin's `simulate_fill` moves the position **unconditionally**: given
`new_position=1.0` it fills at the bar's open whatever the bar did. Demonstrated
on a bar with **no range at all** (`open == high == low == close`): it still
fills.

The harness's `UnfavorableFill` refuses a trade *at* the limit price and requires
one strictly through it; `FavorableFill` accepts it. So the two engines sit at
opposite ends of one axis.

**Not a defect in either.** They are two modelling assumptions, and Task 4's
argument is about which one the loop *scores* on — whichever model a candidate is
scored against is the one it learns to exploit, so the pessimistic one is the
honest choice. What the comparison establishes is the **direction**: nalin's is
the more optimistic, which is the assumption that argument depends on and which
nobody had checked.

### 2.5 `composite_score` — a second objective, now a diagnostic by test

A scoring path any code can import is what `metrics.py`'s import guard exists to
stop. The test asserts `src/core/scoring/scorer.py` does not mention a composite
score, so the objective staying single is a check rather than an intention.

---

## 3. What the fixture allowlist refuses, demonstrated

```
FixtureCalendarRefused: .../calendars/nq-trades-but-really.yaml is in the
fixture registry, which serves only ['es-20250703', 'placeholder'].
That registry exists so two toy datasets do not re-date the artifact
provenance on every committed ledger row. A dataset whose results anyone will
cite does not belong in it.
Register 'nq-trades-but-really' in config/calendars/ instead, and accept that
doing so changes artifact_digests() and makes every existing row's artifact
fingerprint stale. That is a human decision with a stated cost, which is why it
is not made here.
```

Two tests: the refusal names where the file belongs and what registering it there
costs, and `audit_fixture_registry()` returns empty — which catches a calendar
being **committed** to that registry, not only used from it. A calendar filed
there for a real dataset is already the mistake.

---

## 4. Two places the brief's premises were wrong

**"Gowtam's already ... invokes `run.py`."** It does not, and did not. That loop
plans, builds and reviews, and then stops. Nothing in it scored anything or wrote
a ledger row; its entire connection to the harness was
`STRATEGY_FILE = "current.py"` plus a comment saying the built file *"must land
in `src/strategies/current.py`"*. **Neither loop drove scored campaigns.**

Rather than pick between two loops on a property neither had, Task 41 added the
missing step. `Gowtam/L1/loop/evaluate_step.py` writes the built file to
`src/strategies/current.py`, runs `run.py` as a subprocess, restores the file in
a `finally`, and returns the row — **importing nothing from `src.core`**, which
is what makes it evidence rather than convenience. Six tests, including that the
row carries `code_fingerprint`, `artifact_fingerprint`, `artifacts`,
`environment_fingerprint`, `lockfile_digest`, `isolation`, `strategy_digest`,
`fill_log_digest`, `dataset_digest` and `reconciliation.agree` — because "wrote a
row" and "wrote a row anybody can check" are different claims.

**`PROJECT_ROOT` was one level too high.** Three `parent`s from a file two levels
down made it `Gowtam/`, so `candidate_dir` wrote state one directory above the
project whose name the constant carries. Nothing failed, because nothing else
referred to either directory — it would have surfaced the first time two projects
sat side by side under `Gowtam/`. Fixed, with a regression test.

---

## 5. What fits none of the three roles

**`nalin_l1`'s engine core** — `orchestrator/`, `execution/portfolio.py`,
`data/loader.py`, `packaging/`, `strategy/loader.py`. It is not a driver over
this harness, its strategies are ported rather than run, and only two of its
components (metrics, fills) serve as differential references. It stays where it
is, unintegrated and unrun, which is the brief's stated disposition for exactly
this case. Its split now reads a committed calendar and its trial log declares
itself not a ledger row, so it is *safe* unintegrated, which is the most that
can be said without deciding which engine the project has.

**The duplicated `.opencode/agent/*.md`** — three files, byte-identical across
both trees. Deduplicating means choosing which tree owns them, which is the loop
question again. Left, and now named in `architecture.md` §8b.

---

## 6. Still open, and still not mine

- **The harness's `max_drawdown` docstring should say the sign.** `src/core/**`.
- **pydantic**, parked: installing it moves `environment_fingerprint` and
  `lockfile_digest`, recorded on every ledger row. Seven of nalin's test modules
  still error on it.
- **Where a real dataset's calendar goes** — the allowlist now refuses to decide
  it quietly, which was the point.
- **Which engine the project has.** Nothing in Task 40 or 41 decided it, and the
  four roles above are stable whichever way it goes.

---

## 7. Verification

```
tree diff vs baseline        EMPTY                     (at every commit)
pytest tests/ -q             610 passed, 24 skipped
pytest nalin/l1-loop/tests   50 passed, 7 errors
scripts/ledger_check.py      N=0 trials, 0 superseded, 0 environment-drifted
```

The root count moved 590 → 610: twenty tests added in
`tests/test_teammate_integration.py`, none changed or removed.
