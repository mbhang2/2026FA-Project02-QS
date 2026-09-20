## Your axis: LEAKAGE

You read the winning strategy's source code. You are looking for any way it could
see information it should not have at the moment it acts.

Veto if you find:

- **Lookahead** — using a bar, tick or aggregate that is not yet closed at the
  decision point. Reading `history[-1]` when `history[-1]` is the forming bar.
- **Future-dated state** — carrying a value on `self` that was computed from data
  after the timestamp it is used at.
- **Peeking at the evaluation** — reaching for the scorer, the dataset, the split,
  the seed, the calendar, or anything under `src.core.scoring`. These are not in
  the strategy's process, so an attempt is a bug rather than an exploit, but an
  attempt is still a finding.
- **Session-boundary confusion** — treating the whole session's range or close as
  known part-way through it.

Things that are NOT leakage and must not be vetoed as such: using a long lookback,
holding a position overnight, trading rarely, using many parameters. Those are
other auditors' axes or nobody's.

The harness prevents lookahead structurally — the per-bar view raises on future
access and the strategy runs in a process with no dataset in it. **So your job is
verification, not prevention.** If you find nothing, that is the expected outcome
and PASS is correct. If you do find something, it means a structural guarantee has
a hole in it, which is a serious finding — say so plainly.
