# Task 43 — wiring tier 2 to tier 3, and what the DSR cross-check found

Commit range **`4ce8cf4..HEAD`** on `merge-test-michael`. The schema and the
verdict interface are in `docs/design/tier-contract.md`; this is the evidence and
the findings.

---

## 0. Merge direction

Done as instructed, and it was a **fast-forward**: `merge-test-michael` was
already an ancestor of the daksh branch, because the earlier merge had taken it
as a parent. So merging back produced the byte-identical tree — `git diff` between
the two branches is empty — and `user/daksh/audit_agent_mvp` is reset to exactly
`origin/user/daksh/audit_agent_mvp`. **Nothing was pushed and that branch is
untouched.**

---

## 1. The campaign summary schema

`tier2-campaign-summary/1`, written by `scripts/campaign_summary.py`, full field
table in `docs/design/tier-contract.md`. Two real summaries are committed, from
the Task 36 campaign:

```
campaign   task36-history (history)
N          100   {'completed': 99, 'crashed': 1}
gate       REJECT   p 0.6859   observed +0.4222   bar +0.8011
digest     2276bd7af8cdda59
```

```
campaign   task36-control (control)
gate       REJECT   p 0.2658   observed +0.3061   bar +0.5117
digest     39ae15016299c98b
```

Both rebuild to the same digest, checked in CI and by `--check`.

Three choices in it worth defending:

- **A fixed list of seventeen diagnostics, not `dict(**d)`.** A splatted dict
  ships whatever the engine happened to record, and tier 3 starts depending on a
  field nobody meant to publish.
- **Ledger rows by reference** — index, strategy digest, outcome, score, fill-log
  digest. A summary that inlines its evidence is one that can disagree with it.
- **`null_moments` is new.** A deflation cross-check needs the null's location
  and scale; publishing them is what let §3 below be measured on a real campaign
  rather than on synthetic input.

And one absence stated rather than left implicit: **the holdout, by any path.**
A test enumerates every occurrence of the word in the artifact and checks the
set, because its first draft ended in `or True` — a vacuous assertion in the one
test that exists to be non-vacuous.

---

## 2. The tier-3 verdict interface

Three members, no fourth:

```python
class Verdict(Enum):
    PASS = "PASS"                  # nothing found to veto. NOT an approval.
    FAIL = "FAIL"                  # a veto, with reasons
    CANNOT_TELL = "CANNOT_TELL"    # escalates to a human; not a veto
```

`AuditVerdict` is frozen and slotted, its fields are exactly
`{verdict, reasons, checks, escalate_to_human}`, a string verdict raises rather
than being coerced, and a `FAIL` with no reasons raises. There is no `score`, no
`confidence`, no `weight` — **a field that ranks a candidate is a field somebody
will threshold, and a threshold on an auditor's output is an approval with extra
steps.**

> An auditor that can only remove candidates is safe with a completely
> uncharacterised error rate. Its mistakes cost power and can never manufacture a
> discovery. One that can promote must have its error rate measured before anyone
> acts on it, and nobody has measured this one.

Run against both Task 36 arms, both return **CANNOT_TELL and escalate**: the
campaigns were `best-effort` isolated on macOS, which cannot bind a memory
ceiling, so a resource-exhaustion escape cannot be ruled out from the record
alone. That is the honest verdict on those runs and not a defect in them — and it
is the first time the project's own "scored campaigns should run on Linux" note
has been enforced by something rather than written down.

Vetoes are reserved for what makes a campaign *unauditable*: a moved
pre-registration, a record digest that changed after the run, ledger rows that
are neither trials nor admitted harness failures, trials disagreeing about the
code that produced them, holdout data in the summary.

---

## 3. DSR versus `max_of_n` on the Task 36 campaign — **a finding**

| | history arm | control arm |
|---|---:|---:|
| E[max], daksh's closed form | **+0.3910** | +0.1126 |
| E[max], harness's simulation | **+0.5054** | +0.2616 |
| difference | **−0.1144** | −0.1491 |
| observed best | +0.4222 | +0.3061 |
| clears the closed-form bar? | **yes** | yes |
| clears the simulated bar? | **no** | yes |

**On the history arm the two routes disagree about the margin**, and the
direction is the dangerous one: the closed-form bar sits *below* the observed
best. **A deflation computed from one pooled null would have reported a
discovery on a campaign with no signal in it by construction.** The harness's
gate rejected it at p = 0.686.

### Why, and why it is the opposite of Task 42's direction

Task 42 measured the same closed form **high** by +0.017 to +0.036 in Sharpe
units against a 400,000-draw reference. Here it comes out **low** by 0.11–0.15.
Both are true, and the difference between them is the whole point:

- Task 42 compared them on **one homogeneous null**. There, the closed form's
  error is its own approximation error, documented in the literature for small N.
- Here they are compared on a **real campaign**, where Task 32 made the null
  **per candidate**. `max_of_n` takes the maximum over N draws from N *different*
  nulls; the closed form assumes N draws from *one*.

Measured on the history arm's 99 nulls: per-candidate null means run from
**−0.2698 to +0.1031**, against a pooled mean of +0.0154. The maximum is driven
by the candidates whose nulls sit highest, and the pooled mean sits below them —
so a bar built from pooled moments comes out too low. Per-candidate standard
deviations spread 0.0852 to 0.1846 around a pooled 0.1484, which contributes but
is the smaller effect; the locations are what matters.

**Which side I believe: the harness, and not because it is ours.** Its simulation
makes no assumption the campaign violates. The closed form's single-null
assumption is exactly the one the conditional framing abandoned in Task 32, and
applying DSR to a conditional campaign silently reintroduces it.

**Neither number is the gate**, and the summary says so. Both are E[max] — the
DSR-style margin. The gate is P(max ≥ observed) over the whole simulated
distribution, a stricter question; Task 36 already found `deflation_view`
clearing its bar in *both* arms while the gate rejected both. This cross-check
adds that even the margin comparison is unsafe when the nulls are heterogeneous.

**Nothing was reconciled and neither side was made more permissive.** The
cross-check reports, tier 3 escalates, and tier 2's verdict stands.

---

## 4. Purged CV and PBO via CSCV: **not built anywhere**

Searched `src/`, `scripts/`, `daksh/`, `nalin/`, `Gowtam/`. Neither exists.
They are named in the build plan's step 7, in `metric.md` and `types.py` as the
reason Sharpe is carried beside Sortino, and in daksh's `GATES.md` under "Not in
MVP". `purgedcv` and `skfolio` appear in no requirements file.

The embargo that *does* exist is the calendar's, between splits. That is a
different object from purged CV's embargo around each test fold, and having one
is not having the other.

**This is the gap that decides what tier 3 is worth.** `max_of_n` corrects for
how many times you looked. PBO asks whether the *selection procedure*
generalises; purged CV asks whether a fitted parameter survives a fold boundary.
Those are the things a statistics auditor contributes that tier 2 cannot express
— and §3 is a demonstration that the one statistical gate the audit agent *does*
have is the wrong one to apply here.

---

## 5. What the audit record carries

`tier3-verdict.json`, per campaign arm: the verdict and whether it is a veto, the
reasons, the checks including the full deflation cross-check, `can_promote:
false` with its note, `n_trials_provenance`, the campaign id, **the tier-2
summary's digest**, tier 2's own verdict, N, the ledger path, the audit
timestamp, and its own digest.

Against the brief's list, two things are **missing and should be added when tier
3 grows an LLM critic**: the auditor's model id and its prompt digest. Task 36's
finding is the reason — the endpoint served `gemini-3.1-pro-preview` for a
request naming `claude-haiku-4-5`, and nothing recorded it. Today's tier 3 is
pure Python with no model in it, so there is no model id to record and adding an
empty field would be worse than its absence; the moment a critic is wired in,
`Call.model_served` already exists to supply it.

`research/audit_db.json` still records audits rather than trials and still says
so in the file.

---

## 6. What this brief did not anticipate

**The merge was a fast-forward.** The brief describes the reverse merge as
producing "the identical tree while being trivially reversible". It is stronger
than that: `merge-test-michael` was already an ancestor, so there was no merge to
perform and no `--allow-unrelated-histories` needed in this direction.

**PSR cannot be computed at all from a campaign summary.** It needs the
candidate's return series, and a ledger row does not carry one — `diagnostics`
holds aggregates, not `session_returns`. Task 36 recorded that gap; this is the
first place it bites. Closing it means re-running the candidate or changing what
a row records, which is `src/core/**`. So the audit agent's *other* statistic is
unavailable for the same structural reason its DSR is inapplicable, and tier 3's
statistical contribution on this path is currently **zero** — which is worth
knowing before anyone counts it as a gate.

**Both real campaigns escalate, and the reason is the platform.** `best-effort`
isolation on macOS is a known, documented limitation, and it now has a consumer
that refuses to wave it through. Every campaign in this repository was run that
way. Re-running Task 36 on Linux would be the first campaign tier 3 could return
PASS on — and that is a fact about the record, not about the gate.

---

## 7. Verification

```
tree diff vs baseline        EMPTY                     (at every commit)
pytest tests/ -q             639 passed, 24 skipped
scripts/ledger_check.py      N=0, 0 superseded, 0 environment-drifted
campaign_summary --check     current, both arms
```

618 → 639: twenty-one tests added in `tests/test_tier_contract.py`, none changed
or removed.
