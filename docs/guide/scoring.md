# Scoring and the gate

What number each trial gets, what each candidate is compared against, and how a
campaign decides whether its best is a discovery or what the best of N noise
draws looks like.

[Documentation map](../README.md) · deep design: [design/metric.md](../design/metric.md) and [design/pre-registration.md](../design/pre-registration.md) §3-5

> `design/metric.md` is layered by task and its "what is true now" table stops at
> Task 34. The gate campaigns use today is the pre-registration's, described
> below; read the older layers as the record of how it was reached.

## Files

| File | Role |
|---|---|
| `src/core/scoring/scorer.py`, `metrics.py` | The score: Sortino on per-session returns, plus diagnostics |
| `src/core/scoring/randomization/round_turn.py` | The per-candidate null in use (`null_source: round_turn_mc`) |
| `src/core/scoring/randomization/random_trader.py`, `hybrid.py`, `rotation.py` | Other nulls, selectable at registration |
| `src/core/scoring/correction.py` | `max_of_n`, and the diagnostics-only corrections |
| `scripts/nq_campaign.py` (`stage_nulls`, `stage_report`) | Builds the nulls; applies the gate |
| `config/corrections.yaml` | The registered correction settings |

## The score

- **Sortino on per-session returns**, minimum acceptable return 0, downside
  deviation over every session. Per-session returns make strategies that trade
  fifty times a day and once a day comparable.
- **Every session counts, including flat ones**, with an **idle charge**
  (Amendment 3) so sitting out is not free.
- **Exposure floor** (Amendment 6): a candidate exposed in fewer than
  `min_exposed_fraction` of sessions (0.70) is **rejected**, not scored.
- Diagnostics (trade count, skew, kurtosis, profit concentration) are recorded
  and never enter the objective.

## The null and the gate

1. **Each scored candidate gets its own null** (`stage_nulls`): its own round
   turns, resampled `b_null = 10,000` times (`round_turn_mc`, a sign-flip
   Monte Carlo). A candidate is compared with what *its own trading* looks like
   without an edge.
2. **The campaign's best is corrected for N** (`max_of_n`): the bar is the
   distribution of the *maximum* over N null candidates, so trying more costs
   more. Crashed trials are in N.
3. Benjamini-Hochberg, Holm and the expected maximum are computed and kept under
   `diagnostics_only`. **They are not the gate.**
4. Across campaigns, the L2 gate does the same with the campaign as the unit
   (`max_of_m`); see [sessions.md](sessions.md).

Measured: family-wise error on known-null campaigns 0.060 (CI 0.041-0.088) at
q = 0.05 (Task 38). The guarantee is conditional on the campaign's realised
candidates; `design/metric.md` states it in full.

## When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `rejected: exposed in 0 of N scored sessions` | The strategy never traded | A real result, not a bug; the next question may need to force activity |
| "every null sample is EMPTY" | No candidate was scored, or fills were not merged back from worker trees | If nothing was scored, expected; otherwise check `fills/` in the campaign directory |
| `score = -inf` | Too few downside sessions for Sortino | `MIN_DOWNSIDE_SESSIONS` and its Sharpe fallback, in `metric.md` |
| `random_trader` refused for quiet strategies | That null is not calibrated below its activity floor | Register with `null_source: round_turn_mc` |
| `correction_rule 'aligned'` refused | Incompatible with a shared null table | The refusal message names the valid pairs |
