# The contract between the tiers

Three tiers, and the interface between each pair is **a file**.

| tier | what it decides | where it lives |
|---|---|---|
| 1 | one trial's score | `run.py`, `src/core/` |
| 2 | the campaign's accept/reject, corrected for N | `src/core/loop/`, `scripts/llm_campaign.py` |
| 3 | the audit: veto, or escalate | `daksh/audit_agent/` |

**Nothing imports across a tier boundary.** That is the same property the loop
and the harness already have — `Gowtam/L1/loop/evaluate_step.py` drives `run.py`
by writing `src/strategies/current.py` and starting a subprocess, importing
nothing from `src.core`, which is why it could become a second driver without
touching anything. It is what lets either side be replaced without the other
noticing, and it is the only reason three separately-authored trees can be wired
together at all.

Two artifacts carry it:

```
tier 2  ──  runs/campaigns/<id>/<arm>/tier2-summary.json  ──▶  tier 3
tier 3  ──  runs/campaigns/<id>/<arm>/tier3-verdict.json  ──▶  a human
```

Written by `scripts/campaign_summary.py` and `daksh/audit_agent/tier3.py`.
Both are schema-tagged and self-digesting.

---

## `tier2-campaign-summary/1`

What tier 3 needs in order to audit a campaign, named field by field rather than
dumped — an artifact that is "everything we had" is one nobody can tell is
complete.

| block | carries | why tier 3 needs it |
|---|---|---|
| `campaign` | id, arm, profile, dataset, split, budget, q, stopping rule, agent config, **record digest** and whether it moved | the budget and q must be checkable as fixed *before* the first trial, not after |
| `n` | **trials, as the ledger defines them** — every one including crashes — plus the outcome breakdown, harness failures and unaccounted rows | tier 2 owns the correction; this is for display and sanity-checking |
| `gate` | verdict, `p`, `q`, observed best, the **simulated best-of-N bar** and its quantiles, `p_floor`, `at_floor` | so the accept decision can be re-read rather than taken on trust |
| `null_moments` | mean, sd and range of the pooled per-candidate null draws | a deflation cross-check needs the null's location and scale |
| `best_candidate` | index, strategy digest, **the source itself**, score, and the per-trial diagnostics | leakage review cannot review code it cannot see |
| `provenance` | code fingerprint, artifact fingerprint and digests, environment digest, lockfile digest, isolation level, git sha, and whether the code fingerprint was **uniform across trials** | an audit of a run whose code cannot be identified is an audit of nothing |
| `ledger` | the rows summarised, **by reference** — index, strategy digest, outcome, score, fill-log digest | a summary that inlines its evidence is one that can disagree with it |
| `routing`, `deflation_view`, `diagnostics_only` | the hybrid's bucket/rotation split, the DSR-shaped margin, BH and Holm | context tier 3 reports and does not act on |
| `holdout` | `seen: false`, and why | stated rather than merely absent |

The diagnostics carried per candidate are a **fixed list**, not `dict(**d)`:
max drawdown, profit concentration, best-session share, turnover, average holding
period, sessions with exposure, win rate, trade count, commission, gross P&L,
fill-model spread, `null_reference_divergence`, the null assumption, the
seasonality caveat, and the by-weekday / by-month / by-hour breakdowns. A
splatted dict ships whatever the engine happened to record, and tier 3 starts
depending on a field nobody meant to publish.

### What is deliberately absent

**The holdout, by any path.** No holdout data, no path to it, no split identity
beyond the name of the one the campaign ran on. An auditor whose verdict could
depend on data the gate has not already conditioned on is spending a budget
nobody pre-registered.

**The best candidate's return series.** PSR needs it, and a ledger row does not
carry one — `diagnostics` holds aggregates, not `session_returns`. Task 36
recorded that gap; this is where it bites, and closing it means re-running the
candidate or changing what a row records, which is `src/core/**`.

---

## `tier3-audit-verdict/1`

### Three verdicts, and there is no fourth

```python
class Verdict(Enum):
    PASS = "PASS"                  # nothing found to veto. NOT an approval.
    FAIL = "FAIL"                  # a veto, with reasons
    CANNOT_TELL = "CANNOT_TELL"    # escalates to a human; not a veto
```

**PROMOTE is unrepresentable, structurally rather than by policy.** There is no
fourth member, no `score`, no `confidence`, no `weight` — nothing a caller could
threshold, and a threshold on an auditor's output is an approval with extra
steps. `AuditVerdict` is frozen and slotted, a string verdict raises rather than
being coerced, and a `FAIL` with no reasons raises. The emitted record carries
`can_promote: false` explicitly.

The reason is not fastidiousness about enums:

> An auditor that can only **remove** candidates is safe with a completely
> uncharacterised error rate — its mistakes cost power and can never manufacture
> a discovery. An auditor that can promote must have its error rate measured
> before anyone may act on it, and nobody has measured this one.

**`CANNOT_TELL` is required, not a convenience.** Task 35 found a committee
design whose ranking mode forbade "cannot tell"; Task 34's most valuable output
was declining to claim what the data could not support. A `CANNOT_TELL` is **not
a veto** — it escalates to a human and is recorded as having done so.

### What tier 3 checks

*Vetoes* (structural, and each one makes the campaign unauditable rather than
merely suspicious): the pre-registered record moved; the record digest changed
after the run; ledger rows that are neither trials nor admitted harness failures,
so N is short by exactly those rows; trials disagreeing about the code that
produced them; holdout data in the summary.

*Unknowns* (escalate): isolation below `isolated`; no gate verdict to audit; the
best candidate's source absent; and the deflation cross-check below.

### Tier ownership — and where `n_trials` went

**Tier 2 owns the correction. Tier 3 does not re-correct.** `N` is read from the
summary, where it means what `runs/ledger.jsonl` means. The audit agent's older
path computes `n_trials` as `len(glob("experiments/*.json"))` — a number that
shrinks when a file is deleted — and that number is **not used on this path at
all**. `N_TRIALS_PROVENANCE_TIER2` records where N came from, which is what the
provenance constant was added in Task 42 to make visible.

### DSR is a cross-check, never a gate

The closed form in `daksh/audit_agent/engine/stats.py` and the harness's
simulated best-of-N answer the same question two ways. Tier 3 runs both and
reports agreement or disagreement. It does not override, and **neither side is
made more permissive to match the other.** What it found on the Task 36 campaign
is in `docs/reviews/task43/`.

---

## Not built anywhere: purged CV and PBO via CSCV

Searched across `src/`, `scripts/`, `daksh/`, `nalin/` and `Gowtam/`: **neither
exists.** They are named in the build plan's step 7, in `metric.md` and in
`types.py` as the reason Sharpe is carried beside Sortino, and in daksh's
`GATES.md` under "Not in MVP" — and `purgedcv` and `skfolio` are in no
requirements file.

The embargo that *does* exist is the calendar's, between splits. That is a
different object from purged CV's embargo around each test fold, and having one
is not having the other.

This matters for what tier 3 is worth. `max_of_n` corrects for how many times you
looked; PBO asks whether the *selection procedure* generalises, and purged CV
asks whether a fitted parameter survives a fold boundary. Those are the gates a
statistics auditor would contribute that tier 2 cannot express — and they are the
ones nobody has built.
