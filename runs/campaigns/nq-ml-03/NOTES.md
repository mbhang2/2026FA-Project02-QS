# nq-ml-03 — running notes

Written while the campaign runs. Observations only; nothing here changes the
campaign, and no code under `src/core/` or `run.py` is touched while it is in
flight (both are inside `code_fingerprint`).

## Trial-by-trial

| # | outcome | trades | exposed | note |
|---|---|---|---|---|
| 0 | scored −0.1030 (uncharged −0.0973) | 1,732 | 100/100 | the seed's own shape: trades most days |
| 1 | **rejected** | 134 | 48/100 | **Amendment 6's first bite.** Under nq-ml-02's rules this would have been scored |
| 2 | crashed (blocked proposal) | — | — | static check unrepaired after 2 attempts |

## What trial 2 was

Not a strategy fault and not a harness fault: the model returned a file that
failed the static check twice in a row, so `LLMAgent.propose` returned the
blocked-proposal module, `run.py` recorded `crashed`, and the row counts toward
N. That is the pre-registered behaviour (`loop.md`, "How a blocked proposal
becomes a row") and the alternative — skipping the trial — would shrink N by
exactly the proposals that failed.

The specific failure is **numpy-versus-mypy friction**, not strategy logic:

```
error: Incompatible types in assignment
(expression has type "floating[Any]", variable has type "float")  [assignment]
```

A numpy reduction returns `np.floating`, the model annotated the target `float`,
and mypy rejects the assignment. This is the same family that made `nq-ml-01`'s
seed fail `_static_ok` at registration (module-level `len()`, `None`-init, an
un-narrowed `edge`). ViperQ is numpy-heavy, so it can recur.

**Watch the rate, do not act on it.** `nq-ml-01` lost 13 of 20 this way. If this
campaign does the same the result is still an honest campaign — N counts blocked
proposals — but the search will have had few real looks. Stopping and
re-registering *because early trials looked bad* is the forking path the
correction does not cover, so it is not a call to make mid-flight.

**The fix, for after the campaign:** the repair prompt can name the pattern
(`float(np.mean(...))` at the assignment) without naming anything about the
gate. The prompt template is an artifact hashed into the campaign record, so
that edit belongs at a campaign boundary, not inside one. The synthetic probe
(Amendment 7) does not help here: it runs only after the static check passes.

## Launch

First launch aborted before any trial — see `ABORTED-FIRST-LAUNCH.md`. No ledger
row was written and the budget was intact.
