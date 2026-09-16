# Second adversarial pass — the reproductions, and what happened to each

Filed against commit `c1f948c` ("Task 20: close the six adversarial review
findings"). Every script here is a demonstration that ran, not a hypothesis.
They are kept because a review whose rejections are not in the record cannot be
audited, and because a finding described in prose is a finding nobody can re-run.

Run them from the repo root with the project venv: `python
docs/reviews/second-pass/attack_c.py`.

| Script | Finding | Severity | Status |
|---|---|---|---|
| `walk.py` | the reachability probe the others were written from | — | superseded by the walk in `tests/test_boundary.py`, which is wider |
| `attack_c.py` | **1.** `ctx._port.session_bounds.__closure__[0].cell_contents` reaches mutable `_RunState`; writing `avg_price` took Sortino −0.2547 → **+4.2089** | critical | **closed in Task 21.** The score is computed from the fill log. The script still runs and still corrupts the accumulator — it scores `e.blotter` directly, which is no longer what anything is scored on. The regression test in `tests/test_boundary.py` is the same reproduction asserting the *score* does not move. |
| `attack_a.py` | **2.** `ctx._session_bounds.__self__` is the raw dataset through `_SealedDataset.__getattr__` | critical | **closed in Task 25**, structurally. The strategy runs in its own process and there is no dataset in it to forward. The `gc.get_referents` walk re-run *inside* that process over 20,794 objects finds no engine, dataset, blotter or queue. |
| `attack_i.py` | **3.** `ctx._queue.cap` and `.actions` are mutable; 200 submits past a cap of 64 | critical | **closed in Task 25**, structurally. The cap is enforced on the engine side of a boundary the child cannot cross; order ids are pre-allocated per callback from a block the size of the cap. |
| `attack_h.py` | **4.** Task 19's "no anti-conservatism" is split-direction dependent; worst ratio 2.64 | high | **open.** Tasks 31–33 replaced the operating null with a conditional, per-candidate one, which is the same territory — but this finding has not been re-run against it, so it is not closed. |
| `attack_fg.py` | **5.** the null is 9–24× anti-conservative for selective strategies (25% exposure → 24.40× at far=0.001) | high | **open**, same as 4. The profile-indexed null buckets on exactly this — active fraction and downside fraction — but the finding's own reproduction has not been re-run. |
| `attack_e.py` | **6.** `scripts/agent_check.py` is an oracle: 18 probes recovered peak realized P&L to ±0.0002 with 0 trials counted | high | **closed in Task 23.** The check no longer executes strategy code -- `compile()`, an AST pass and mypy, all properties of the source text. `docs/reviews/task23/oracle_after.py` runs the same search against two strategies whose peak P&L differs by 4.39 and gets identical answers from both. |
| `attack_d.py` | **7.** null tables, config, ledger and strategy source are writable, and the scoring fingerprint does not change when `data/calibration/empirical-null.json` is rewritten | medium-high | **detection closed in Task 22, prevention closed on Linux in Task 26.** `integrity.artifact_digests()` hashes the tables, calendars, profiles, manifest and strategy source before the run and again after scoring; a mid-run write invalidates the trial as `invalidated_artifacts_mutated`. Under bubblewrap the write is refused with `EROFS` before detection has to fire. On macOS the sandbox is `best-effort` and detection is the only layer — which is why every run record carries its isolation level. |
| `attack_b.py` | T+1 eligibility | — | **could not break it.** Recorded because a boundary nobody managed to cross is evidence, and only if the attempt is written down. |

`docs/design/architecture.md` §10 is the authority on what is still open. As of
Task 34 that is findings 4 and 5, plus finding 7's writability on macOS.

Named for a third pass and not yet attempted: a candidate that measures
autocorrelation can detect rotated data and sandbag its own null.

## What both passes missed

**Network egress.** The strategy process could open a socket, and a strategy that
fetched real prices for the evaluation dates would have produced a false
discovery that every defence in Tasks 21 through 25 is orthogonal to: genuine
blotter, stable dataset digest, clean fingerprint, and a replay that reproduces
because it fetches them again.

Neither pass reported it. Both briefs framed the boundary as an in-process one,
and both passes duly reasoned about the object graph -- `__self__`,
`__closure__`, `gc.get_referents`. The reachability walk is a good tool and it
answers exactly one question. It cannot ask where the data came from.

Worth keeping as the shape of the miss rather than the miss itself: a review
inherits the threat model of its brief. Closed in Task 26
(`docs/reviews/task26/network_probe.py`), and the boundary is now described in
`architecture.md` as a data boundary rather than a process boundary.
