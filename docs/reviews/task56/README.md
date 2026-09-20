# Task 56 — `nq-mvp-01`, the first campaign on real data

The artefacts are `runs/campaigns/nq-mvp-01/`. The runner is
`scripts/nq_campaign.py`; the protocol is `docs/design/pre-registration.md`,
registered 2026-09-19 19:49:46, before this ran.

## The configuration

| | |
|---|---|
| data | NQ trades, **train** split, 384 sessions |
| N | 50, `q` = 0.05 |
| null source | **`round_turn_mc`**, B = 10,000 (§3b) |
| correction | `max_of_n` |
| commission | **0.10 index points per side** — $2.00/side, $4.00 round turn, Michael's live figure |
| model | `claude-haiku-4-5`, preflight verified serving and echoing its own id |
| hypothesis | `config/hypotheses/intraday-continuation.md`, digest `38c817dc28bab487` |
| champion | carries forward, advancing on a strict improvement |
| cost | 3 h 01 m for the trials, **145 s** for the nulls |

## The result — all five views reject

```
N 50        48 completed, 2 crashed, 0 unaccounted

uncorrected   best +0.0256, nominal FAR 0.2376
BH            0 discoveries of 50
Holm          0
deflation     bar +0.1665, margin -0.1409, clears False
GATE          p 0.8236, 5% bar +0.0980            REJECT
tier 3        CANNOT_TELL, escalate to a human
```

**The best of 50 candidates scored +0.0256. The best of 50 worthless candidates
averages +0.0608.** The observed best is *below* what luck alone produces, so the
`sortino > 0` floor never came into it — the p-value did the work.

Tier 3's reason is `isolation level is 'best-effort'`: macOS cannot give the
strategy process a memory ceiling, so a resource-exhaustion escape cannot be ruled
out from the record. That is the verdict working. `task53-pilot-linux-pinned` is
the only PASS in this repository and the only difference is Linux.

## What the search actually found, which is the interesting part

| trial | trades | score | net |
|---:|---:|---:|---:|
| 0 | 14,565 | −0.7271 | −2,585.7 |
| 6 | 1,472 | −0.3638 | −332.2 |
| 33 | 58 | −0.1342 | −15.9 |
| 39 | 57 | −0.0304 | −3.0 |
| 42 | 2 | −0.0034 | −0.1 |
| **44** | **3** | **+0.0256** | **+0.63** |

Turnover fell by more than three orders of magnitude and the score improved
almost monotonically the whole way. **The champion makes three trades in 384
sessions and nets 0.63 index points — about $12 across eighteen months.**

The agent did not find an edge. It discovered that when every trade costs money
and there is no edge, the way to maximise the score is to stop trading. Two
proposals (#7, #22) went further and traded nothing at all, which the scorer
rejects for having no dispersion.

**That is a finding about the metric, not about the market**, and it is worth
stating plainly: Sortino on per-session returns rewards inactivity when the cost
of activity is real and the edge is not. The gate refused to call it a discovery,
which is the system behaving correctly, but a search that converges on doing
nothing has stopped searching for what we wanted it to search for.

The hill-climb itself works, and is visible in the proposals, which reason about
prior trials by index:

> *#44: "#42: −0.0034 — best score but only 2 trades (over-filtering). #43:
> −0.0495 — regression when relaxing range_stability_threshold to 0.35. Key
> insight: #42's success with minimal trades suggests trade quality over quantity,
> but 2 trades is clearly too few."*

## Three defects, all found by real data rather than by tests

**1. The null measured a different statistic from the score.** Five candidates
traded so little that the scorer fell back to `sharpe_per_session` — **including
the champion, at three trades and one downside session** — while their nulls were
Sortino distributions. Task 29 closed this at the FAR level, charging every trial
`max(far_sortino, far_sharpe)` precisely so a strategy cannot choose which null
judges it; that fix does not reach `max_of_n`, which compares raw scores, and
`round_turn.null_sample` inherited the gap.

Per-trial metric on this campaign: 43 Sortino, 5 Sharpe, 2 rejected.

**2. 2.14 % of null draws were infinite.** A sign-flip in which every traded
session comes out positive has zero downside deviation, and bare Sortino returns
`+inf` there. 8,999 infinities out of 420,000 put `null_max_mean` at infinity.

Both are the same root — the null called `metrics.sortino` instead of
`scorer.score_returns`, the whole rule — and the same one-line fix.

**3. Tier 3 was handed the wrong object.** The contract is that tier 2 writes
`tier2-summary.json` and tier 3 reads it. The runner passed `summarise()`'s output
directly, so tier 3 reported *"the best candidate's source is absent"*, which was
true of the object and false of the campaign.

**The first report would have been wrong on all three.** The p-value moved from
0.8302 to 0.8236 and the verdict did not change — which is luck, not vindication.

## What this result does not say

**It is not the measured FWER.** 0.060, CI [0.041, 0.088] is the *conditional*
null with `max_of_n` at R = 150 (Task 38). This campaign used the sign-flip null,
whose error rate was unmeasured when this ran — `docs/reviews/task57/` is the
measurement. The summary carries `error_control_note` saying so.

**The agent's token cost for this run is lost.** The driver attached `drain_log()`
to the in-memory row only; it writes a sidecar now, from the next campaign on.
