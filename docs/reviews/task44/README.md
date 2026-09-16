# Task 44 — the name directories are gone

Commit range **`7478534..HEAD`**. `Gowtam/`, `nalin/` and `daksh/` no longer
exist. The target tree was committed first, as `docs/design/repo-layout.md`, so
the rest is reviewable as a diff against intent.

---

## 1. Target versus produced

| target | produced | difference |
|---|---|---|
| `loop/` | `loop/` + `loop/{schemas,opencode_bridge,agents}/` | none |
| `audit/` | `audit/` + `audit/{engine,agents,commands,skills}/` | none |
| `alternatives/l1_engine/` | as stated, with `calendars/`, `market_data/`, `agents/` | none |
| tests under `tests/audit/`, `tests/alternatives/` | `tests/audit/`, **`tests/l1_engine/`** | **renamed — see §5** |

One deviation, and it was forced. Everything else landed where the layout said.

## 2. The fingerprint boundary: **there wasn't one**

The brief permits a fingerprint-affecting commit and asks for it to be a single
one. **No commit needed to affect a fingerprinted path**, because the layout was
designed so nothing lands in `src/core/**`, `run.py`, `config/**` or `data/**`.

```
tree diff vs baseline        EMPTY   at every one of the five commits
ledger_check                 N=0 trials, 0 superseded, 0 environment-drifted
campaign_summary --check     current, both committed arms
```

**No rows went stale. No supersession was recorded because there was nothing to
supersede. The baseline was not re-cut.** Every role that needed a new home was
outside the fingerprinted set, which was a constraint on the design rather than
something discovered afterwards.

## 3. Every relative root walk in the repository was invalidated

Six of them, across all three trees, and **not one would have failed loudly**:

| file | was | became | what it pointed at meanwhile |
|---|---|---|---|
| `audit/config.py` | `parents[2]` | `parents[1]` | `~/Desktop/Practicum`, the directory above the repo |
| `loop/paths.py` | `parent.parent` | `parent` | the repo root instead of the package |
| `alternatives/l1_engine/loop/paths.py` | ×3 | ×2 | `alternatives/` |
| `.../splitting/windows.py` | ×3 | ×2 | `alternatives/`, so the fixture calendars vanished |
| `.../data/download_r2.py` | ×3 | ×2 | `alternatives/secrets/r2.env` — it would have printed "please fill this out" |
| `.../data/loader.py` | ×3 | ×2 | `alternatives/market_data/raw` |

`loop/paths.py` is the third correction to the same constant: three `parent`s at
`Gowtam/L1/loop/paths.py` resolved to `Gowtam/`, Task 41 made it two, and moving
the package made two wrong as well. **A relative walk is a fact about where a
file sits, and a file that moves invalidates it silently.** The comment in that
file now records all three.

## 4. What could not be made to run

**daksh's eight tests.** They need demo fixtures under `research/examples/` —
three experiment ids the README documents, `DEMO.md` walks through, and the tests
assert verdicts for. Those files exist **in no branch**, and nothing gitignores
them. Verified against a throwaway worktree at the audit agent's own root commit,
which fails identically, so this is not the merge and not the restructure.

Skipped with a reason naming the three ids and saying to ask the author. **Not
fabricated** — inventing files whose expected verdicts someone else's tests
assert is inventing the evidence the tests exist to check. The other four of
daksh's tests are not skipped; skipping them would be losing four real tests to
tidy a count.

**The second engine's seven pydantic modules.** `pytest.importorskip` with a
reason naming the cost: pydantic is not in `requirements.lock`, and installing it
moves `environment_fingerprint` and `lockfile_digest`, both recorded on every
committed ledger row, where `ledger_check` currently reports
**environment-drifted: 0**. Parked since Task 40 and still a human's call.

**Paper retrieval**, which Task 43's brief describes as loop-side work, **does
not exist anywhere in the tree**. There was no file to place. When it is written
it belongs in `loop/`, because it feeds prompt assembly.

## 5. The one deviation, and it is the good kind

The layout said `tests/alternatives/`. That directory, with an `__init__.py`,
makes `tests.alternatives` a package — and with `tests/` on `sys.path`,
`alternatives` then resolves to *it* rather than to the real package at the repo
root, so every `from alternatives.l1_engine import ...` fails with
`ModuleNotFoundError: No module named 'alternatives.l1_engine'`.

**That is the two-packages-called-`src` collision from Task 39, rebuilt inside
`tests/` while being removed everywhere else.** The suite caught it on the first
run. Renamed to `tests/l1_engine/`, with the story in its `__init__.py` so the
next person does not put it back.

## 6. The test count

**693 passed, 39 skipped** — from 639 passed / 24 skipped before the task.
Fifty-four tests joined the root suite by moving into it; none was written, none
deleted, none weakened.

| skips | n | reason |
|---|---:|---|
| `tests/audit/` | 8 | demo fixtures under `research/examples/` exist in no branch |
| `tests/l1_engine/` | 7 | `pydantic` absent from the lockfile; installing it moves two digests on every row |
| `tests/test_nq_adapter.py`, `test_live_bucket.py`, `test_task7_interface.py` | 23 | R2 credentials not in the environment — pre-existing, unchanged |
| `tests/test_remote.py` | 1 | pre-existing |

## 7. The end-to-end run

`docs/reviews/task44/end_to_end.py`, N = 5 on synthetic training data with the
stub proposer. Deterministic, scratch directory, and far below anything that
could support a claim — which is the point: this tests seams, not statistics.

```
tier 1+2   N 5   outcomes {'completed': 5}   unaccounted 0
           gate computed: False  (no per-candidate nulls at N=5, by design)

driver     loop/evaluate_step wrote src/strategies/current.py, ran run.py
           outcome completed   score -0.1113
           verifiable: code_fingerprint yes, artifacts yes,
                       isolation best-effort, reconciled True

tier 2     summary written, digest 4f97f869d51a5749

tier 3     CANNOT_TELL, escalated
             - isolation level is 'best-effort'
             - this campaign has no gate verdict to audit
           veto: False   can_promote: False
```

All four tiers ran and each produced the artifact the next one reads. Tier 3's
verdict is the correct one: an N=5 campaign has no per-candidate nulls, so there
is no gate verdict to audit, and saying so is what `CANNOT_TELL` is for.

**It found a real gap.** `campaign_summary.build` read the arm block only from a
multi-arm `report.json`, which `scripts/llm_campaign.py` writes and a single
campaign does not. The first run came out with an empty outcome map and no gate —
**which looked like the campaign had no gate rather than like the summary had no
source.** Both shapes are read now, and the committed Task 36 summaries still
rebuild to their stored digests.

## 8. What the brief did not anticipate

- **The fingerprint boundary was avoidable entirely.** The brief plans for rows
  going stale and for a re-baseline. Neither was needed. Worth knowing before the
  next restructure assumes the cost is unavoidable.
- **Six relative walks broke silently, not one.** The brief anticipates adapting
  imports; the walks are the part that does not announce itself, and
  `download_r2.py`'s would have looked like a configuration problem.
- **A test directory can shadow a source package.** §5. The mitigation is the
  same one Task 39 named for `src`, applied one directory further in than anyone
  was looking.
- **The audit agent's `__main__.py` CLI** (`python -m audit list|run|ticket`) is
  untested at every path that needs the missing fixtures, so the restructure's
  effect on it is unverified beyond the module importing. Named rather than
  claimed.
