# Question fidelity — does a candidate answer the question it was registered under?

2026-11-05. Three pieces, built and measured in this order:

1. **The post-hoc check always runs** — `audit/committee/review.py` retries a seat
   whose reply was unusable, and `scripts/committee_retry.py` re-runs seats that
   never ran, beside the record.
2. **Mechanical checks per trial** — `src/core/loop/fidelity.py`: a constraints
   file beside each hypothesis, code checks before the trial, behaviour checks on
   its fills after.
3. **A shadow model reviewer per trial** — `scripts/fidelity_judge.py`, for the
   requirements no mechanical check can express. Records only.

## 1. Nine economics seats had never run

The economics seat is the one that asks whether the champion implements its
question. Of 27 committees, 9 never produced an economics verdict:

| cause | campaigns |
|---|---|
| gateway 502 (already retried; s07-02/03 re-run by hand on 2026-10-10) | s06-01, s07-02, s07-03 |
| reply not JSON, not retried | ml-02, s03-02, s04-03 |
| whole budget spent reasoning (`finish_reason=length`), not retried | t72b-01, t72b-02, t72b-03 |

Plus two committees that failed outright (s02-01 timeout, spx-clean-01 key budget).
After the fix, `committee_retry.py --all` gave every one of them a verdict;
s03-02 needed 5 attempts and s06-01 needed 4, so the unusable-reply retry was
load-bearing. Results are in each campaign's `committee-seat-retry.json`:

| campaign | economics on re-run |
|---|---|
| nq-ml-02 | PASS |
| nq-s02-01 | PASS |
| nq-s03-02 | PASS |
| nq-s04-03 | CANNOT_TELL — genuine: the campaign scored no trial, so there is no winner |
| nq-s06-01 | **VETO** — trades the overnight Globex session, not the regular-hours open |
| nq-t72b-01/02/03 | PASS |
| spx-clean-01 | VETO (and robustness VETO) |

A latent defect surfaced on the way: `stage_report` built buy-and-hold from the NQ
adapter, which calls `sys.exit` when bucket credentials are absent, so a report
without them died before the committee ran. Now recorded as unavailable.

**A correction to how this work was motivated.** `nq-flowcont-01`'s economics veto
opens "The strategy tests the OPPOSITE direction from the hypothesis" and two
sentences later says "This is correct directionally". The code follows the flow.
It is not a drift; the seat contradicted itself and vetoed on costs. The seat's
real drift findings are three: flowcont-5m, orderflow-absorption-02, s04-02.

## 2. Mechanical checks over every historical trial

Constraints for 17 hypotheses were written by an agent that was shown the
hypotheses and the API docs and **not** `runs/`, the reviews or git history, so
they could not be fitted to known outcomes. Every constraint quotes its
hypothesis verbatim (`load()` enforces it). `eval_fid.py` here runs them over
every trial's stored source and fills (fills are in the pre-release archive;
`runs/campaigns/**/fills/` is not published).

Champion = best-scoring trial. "Off" = scored trials with any violation.
Economics = the committed verdict, then the re-run.

| campaign | off / scored | champion at 95% | champion at 100% | economics seat |
|---|---|---|---|---|
| nq-flowcont-5m | 50/50 | hold-at-least-5m (median hold 21 s) | same | VETO |
| nq-orderflow-absorption-01 | 7/7 | level-from-volume-profile | same | — |
| nq-orderflow-absorption-02 | 85/85 | level-from-volume-profile | same | VETO |
| nq-s04-02 | 8/8 | flat-by-rth-close (74 of 337 open after 15:15) | + entry window | VETO |
| nq-s06-01 | 80/80 | enter-in-regular-hours (1265 of 2627 outside) | same | failed → **VETO, same reason** |
| nq-s06-02 | 57/57 | enter-in-regular-hours | same | VETO (costs) |
| nq-s06-03 | 26/26 | enter-in-regular-hours | same | — |
| nq-s01-continuation | 18/18 | within-one-session (105 of 257 carried) | same | PASS |
| nq-s02-01 | 16/17 | within-one-session (32 of 466) | same | failed → PASS |
| nq-t72b-01 | 9/11 | within-one-session (46 of 540) | same | failed → PASS |
| nq-s03-03 | 1/16 | within-one-session (59 of 970) | same | PASS |
| nq-s03-01 | 0/13 | clean | within-one-session (33 of 1096) | PASS |
| nq-s04-01 | 2/11 | clean | entry window (1 of 375) | VETO (DST) |
| nq-t72b-03 | 2/16 | clean | within-one-session (46 of 2684) | failed → PASS |
| nq-s07-01/02/03, s05b-01, flowcont-01, ml-*, orderflow-01/02, mvp-01, t72b-02, s03-02 | 0–1 each | clean | clean | various |

**Reading it.**

- All three of the seat's real drift findings are caught, mechanically, before
  the trial would have been allowed to become champion.
- **s06-01/02/03** make about half their entries outside regular hours under
  "Not the overnight or Globex session". The re-run seat vetoed s06-01 for
  exactly that. The mechanical check found all three.
- **The intraday-continuation family** carries positions across session
  boundaries under "Within a single trading session … over the rest of the
  session". The seat passed every one of them. This is the most consequential
  disagreement and it is a reading of the hypothesis, not of the code: the
  constraint is the blind agent's, it is listed as an implied reading in its
  report, and a human should confirm it before `enforce` is used on that family.
- **100% → 95%.** The first default failed s04-01 for one entry in 375 and three
  intraday campaigns for 2–3% of round turns. Chosen on this table, and stated
  as such in `fidelity.DEFAULT_SHARE`.

## 3. The shadow reviewer

`judge-planted-cases.json`: three faithful champions and four planted
violations of one requirement each, every case run twice, `gemini-2.5-pro`.

| | result |
|---|---|
| planted violations called VIOLATED | **8 / 8** |
| faithful requirements called VIOLATED | **0 / 10** |
| CANNOT_TELL | 0 / 18 |
| two runs agree | 9 / 9 |

The plants: flow orders swapped (a fade), target 2× the stop (not symmetric),
buying after the rise (not "sells after a price rise"), the opening range
replaced by the current bar.

`judge-natural-cases.json`: real champions the economics seat vetoed for
reasons only a reader can see.

| champion | requirement | seat | reviewer |
|---|---|---|---|
| s07-02 | "must not let an undecided departure speak for either account" | VETO (time-outs not separated) | HOLDS |
| s07-03 | same | VETO | CANNOT_TELL |
| mvp-01 | not a regime model; "large" from the session | VETO (fades; question undirected) | HOLDS, HOLDS |

**Reading it.** Blatant violations are caught reliably and nothing faithful was
flagged — on seven files, which is a sanity check, not an error rate. On the
subtle real disputes it does not agree with the seat, and the seat is not ground
truth either (its mvp-01 veto looks wrong). That is the case for **shadow**: it
records per trial from now on, and the record is what a decision to let it act
would need.

## What changed in the procedure, and what did not

- `fidelity_policy: record` is the default. Code repairs (not trials; nothing
  executes) and history notes. N, the champion and the gate are untouched.
- `fidelity_policy: enforce` is refused until **Amendment 14** is signed
  (`docs/design/pre-registration.md`). It blocks unrepaired code violations,
  keeps off-question trials from becoming champion, and takes the gate's observed
  best over on-question trials with every null kept — conservative by
  construction; the argument is in the amendment.
- `fidelity_judge: shadow` is the default and has no blocking mode.
