# Task 23 — the oracle, after

`oracle_after.py` is `docs/reviews/second-pass/attack_e.py` run against the
static check. It is kept beside the original for the same reason the originals
are kept: a fix described in prose is a fix nobody can re-run.

    python docs/reviews/task23/oracle_after.py

The single-search version of this demonstration is misleading and the script says
so: a constant answer still drives a binary search's interval to one end, and a
narrow interval looks like a result. So it runs two searches, against strategies
whose realized P&L differs by construction, and compares. Measured on the
committed synthetic check dataset:

| | long side | short side |
|---|---|---|
| true peak realized P&L | -0.0100 | +4.3800 |
| distinct answers over 18 probes | `[True]` | `[True]` |
| converged interval | `[-50.0000, -49.9996]` | `[-50.0000, -49.9996]` |

The two searches are byte-identical on strategies 4.39 apart. Before Task 23 the
same 18 probes recovered the number to +/-0.0002.

36 ledger rows were written across both searches, all `purpose: check`, none
recording that strategy code ran, none counting toward N. That part is unchanged
and is the point: the check is still logged, it just no longer answers a question
about the data.
