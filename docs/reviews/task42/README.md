# Task 42 — `daksh/audit_agent/` reviewed, under Task 41's framework

The audit agent arrived on a **root commit with no shared history**, merged with
`--allow-unrelated-histories`, after the Task 41 overlap review had settled six
overlaps between the harness and the two trees that existed then. This is that
review, applied to the fourth tree, with the same invariants and the same three
roles.

---

## 1. Its role: the committee

`docs/design/loop.md` records step 7 of the build plan — *"Committee as a second
accept tier … promotion runs four auditors in parallel … any one can veto"* — as
not existing. This tree is it: **frozen Python gates first, then a read-only LLM
critic that cannot override a FAIL.** Seven gates in execution order (backtest,
leak, reproduce, PIT, execution, freeze, stats), a strict action mapping
(`kill` / `modify` / `pause`), and `PROMOTE` deliberately out of scope because it
is a capital decision.

**What makes it more than a fourth engine.** Its gates *audit empirically* what
the harness enforces *structurally*:

| its gate | what the harness does instead |
|---|---|
| **leak** — corr(signal[t], return[t]) at delay 0, FAIL at \|ρ\| ≥ 0.5 | `ctx.history()` contains only closed bars; reaching further raises `LookaheadError` |
| **PIT** — bump `closes[t+1]`, assert `signal[t]` unchanged | the same property, made unrepresentable rather than tested |
| **freeze** — dates and costs against `PROTOCOL.md` | `config/calendars/`, whose commit date *is* the pre-registration |
| **stats** — PSR, DSR against `n_trials` | `correction.py`: BH, Holm, `max_of_n`, `deflation_view` |

That is a **differential reference on the guarantees**, which is worth more than
a differential reference on arithmetic. A structural defence that is never tested
empirically is a defence nobody has seen fail. This tree can make the harness's
no-lookahead claim falsifiable — `engine/strategies/lookahead_cheat.py` is a
deliberate cheat, exactly the positive control such a test needs.

It imports nothing from `src.core`, asserted over import statements.

---

## 2. Finding 1 — the two deflated-Sharpe implementations disagree

Both compute **E[max of N draws from the null]**, the quantity DSR deflates by.
`daksh/audit_agent/engine/stats.py` uses Bailey & López de Prado's closed form:

```
sigma * [(1 - gamma) * z(1 - 1/N) + gamma * z(1 - 1/(N*e))]
```

`src/core/scoring/calibration/null.expected_max` simulates it instead — 2,000
draws at seed 7 — because the closed form exists only for Sharpe and the harness
needs the same bar for Sortino.

Adjudicated against a 400,000-draw reference (se ≈ 0.001):

| N | reference | harness (2,000 draws) | closed form |
|---:|---:|---:|---:|
| 5 | 1.16294 | 1.14333 (**−0.020**) | 1.19259 (**+0.030**) |
| 10 | 1.53838 | 1.52684 (−0.012) | 1.57460 (+0.036) |
| 25 | 1.96535 | 1.94159 (−0.024) | 1.99715 (+0.032) |
| 100 | 2.50729 | 2.50147 (−0.006) | 2.53060 (+0.023) |
| 400 | 2.96815 | 2.95941 (−0.009) | 2.98480 (+0.017) |
| 2 | 0.56376 | 0.53183 (−0.032) | 0.51976 (−0.044) |

**Which side I believe: the harness, on the number.** It is closer at every N
tested. The closed form is biased **high** by +0.017 to +0.036 in Sharpe units
for N ≥ 5 — its own approximation error, documented in the literature for small
N — while the harness's residual is Monte Carlo noise at 2,000 draws.

**Why it is recorded rather than corrected.** The closed form's bias sets the
deflation bar too high, so a real result is more likely to be called unproven
than the reverse. That is the conservative direction for an audit gate, which is
the safe way for a gate to be wrong. Changing daksh's implementation would make
their gate *more permissive*, which is not a change to make on someone else's
behalf.

**What this says about the harness's own number.** Its Monte Carlo error at 2,000
draws (≈0.01 at moderate N) is the same order as the closed form's bias. Raising
the draw count is cheap and would shrink it — and it is an edit to
`src/core/**`, so it is reported here rather than made.

---

## 3. Finding 2 — `n_trials` is a count of files in a directory

`engine/audit.py`:

```python
n_trials = len(list((ticket_dir / "experiments").glob("*.json"))) or 1
```

Three differences from this project's N, none of them a defect in daksh's code
so much as a different meaning of the word:

- **Reconstructed after the fact.** `runs/ledger.jsonl` is written as trials
  happen, including the crashed ones. CLAUDE.md's phrasing is "logged rather than
  estimated after the fact", and a filesystem count is an estimate made later.
- **It shrinks when a file is deleted.** Remove a losing experiment and the
  deflation bar drops, improving whatever survives. Nothing detects this.
- **It is per ticket.** This project's N is global across every dataset, because
  otherwise adding a dataset silently buys a fresh holdout — pre-registered in
  the build plan while it was still abstract.

An auditor deflating by the trials it can see is reasonable. Calling that number
`n_trials` without saying which trials is not, because **a deflated Sharpe quoted
against the wrong N looks corrected and is not**. `N_TRIALS_PROVENANCE` now
travels in every packet that reports it, and a test pins that it does.

---

## 4. Finding 3 — a third record with an `n_trials` column

`research/audit_db.json` records **audits**, not trials. It carries `dsr`, `psr`
and `n_trials`, which is precisely why it has to say so: a directory of rows with
an `n_trials` column is something somebody will count.

Same disposition as `nalin_l1`'s trial log in Task 40, same reasoning: these rows
could not become trial rows by relabelling, because a trial row asserts a
`RunTicket`, a code fingerprint, artifact digests, a dataset digest, an isolation
level and a reconciled reconstruction. `NOT_A_TRIAL_LEDGER` is stamped on every
write, so a file written before this gains the block instead of staying
unlabelled.

---

## 5. Finding 4 — the demo data was never committed

**All eight of the audit agent's own tests fail**, with `FileNotFoundError` for
`research/examples/…`. Verified against a throwaway worktree at its own root
commit `3d55cf6`: identical 8 failed / 4 passed, so this is not the merge.

`research/examples/` appears in **no branch of this repository**, and nothing in
any `.gitignore` excludes it — the files were simply never committed. The README
documents three demo ids, `DEMO.md` walks through them, and the tests assert
their expected verdicts (`kill`, `pause`), so the component is fully specified
and cannot be run or verified as committed.

**Not fixed here.** Fabricating experiment fixtures whose expected verdicts are
asserted by someone else's tests would be inventing the evidence the tests exist
to check. The author has this data; the repository does not.

The root `pytest` does not collect these tests — `testpaths = ["tests"]` — so CI
is unaffected either way.

---

## 6. What was changed, and what was not

Changed, all inside `daksh/`:

- `engine/audit.py` — `N_TRIALS_PROVENANCE`, carried in every packet that reports
  the number.
- `engine/store.py` — the ledger declares it is not the trial ledger, stamped on
  every write.
- `tests/test_teammate_integration.py` (root) — eight tests: the expected-max
  disagreement with its sign and size pinned, `n_trials`'s provenance, the ledger
  block including the upgrade path for an older file, and the no-import property.

Not changed: **the gates, the action mapping, the strategies, the backtest, the
closed form.** None of that is mine to decide, and the two numbers that are wrong
are wrong in the direction a gate should be wrong in.

Nothing in `src/core/**`, `run.py`, `config/**` or `data/**`. Tree diff empty.

---

## 7. Still open

- **The harness's `expected_max` draw count.** 2,000 draws is ≈0.01 of noise at
  moderate N, the same order as the bias found above. `src/core/**`.
- **`research/examples/`** — the audit agent's demo data.
- **Whether the audit agent becomes the committee in fact.** It is the committee
  in shape. Wiring it to `runs/ledger.jsonl` as the source of `n_trials`, and to
  campaign records as the source of the freeze check, is the work that would make
  its verdicts about this project's trials rather than about a directory. That
  is a design decision, not a filing one.
- **The lookahead positive control.** `lookahead_cheat.py` against the harness's
  structural defence is the most valuable single test in this tree and it is not
  written. It needs the demo data above.
