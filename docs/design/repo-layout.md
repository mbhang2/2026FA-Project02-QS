# The target tree

Written **before** Task 44 moved anything, so the restructure is reviewable as a
diff against intent rather than as a pile of renames.

The rule: **a directory is a role, never a person.** `Gowtam/`, `nalin/` and
`daksh/` say who wrote something, which is what git already records and what a
reader never needs. What a reader needs is whether a file is on the scored path.

```
run.py                       tier 1 entry point. The immutable one.
config/                      run profiles, pre-registered calendars, the agent
                             prompt, the macOS sandbox profile. FINGERPRINTED.
data/                        committed artifacts: the bucket manifest, the
                             calibration tables. FINGERPRINTED.
src/
  core/                      THE scored engine, THE metric, THE gate.
                             Tiers 1 and 2. FINGERPRINTED.
  strategies/                current.py -- the only agent-writable file at run
                             time -- plus named ported strategies. current.py is
                             FINGERPRINTED; the others are not.
loop/                        the agentic driver. Writes the strategy file and
                             invokes run.py; imports nothing from src.core.
audit/                       tier 3. Reads a campaign summary, may only veto.
                             Imports nothing from src.core, and nothing imports
                             it.
alternatives/                kept, never scored.
  l1_engine/                 a second, vectorised backtest engine with its own
                             loop, metrics, fill model, splits and fixtures.
scripts/                     build-time. Produces committed artifacts; never
                             imported by src/core.
runs/                        append-only records: the ledger, fill logs,
                             campaign directories, stored sources.
research/                    hypotheses and one-off experiment scripts. Input to
                             experiments, as against docs/, which describes the
                             system that runs them.
tests/                       one suite, one count. Every component's tests,
                             including the ones that cannot pass.
docs/                        design, reviews, plans, tasks, agent context.
agent-patterns/              mentor-supplied multi-agent patterns, reviewed in
                             Task 35 and adopted only as conventions.
secrets/                     gitignored.
```

## Why each destination, for the three trees being dissolved

| from | to | the role that put it there |
|---|---|---|
| `Gowtam/L1/loop/`, `opencode_bridge/`, `schemas/`, `.opencode/` | `loop/` | It is the one driver that reaches this harness. `paths.STRATEGY_FILE = "current.py"`, and `evaluate_step.py` writes that file and runs `run.py` as a subprocess. A second independent driver over one interface is evidence the interface is real. |
| `daksh/audit_agent/**` | `audit/` | Tier 3. Frozen gates, then a read-only critic that cannot override a FAIL; `tier3.py` reads `tier2-summary.json` and emits a verdict whose type has no member meaning approval. |
| `nalin/l1-loop/nalin_l1/**`, its calendars, market data and fixtures | `alternatives/l1_engine/` | A **second engine**, not a second implementation of this one. Vectorised next-bar positions against event-driven intents is a different research object; its metrics, fill model and splits come with it. Kept as a differential reference, which Tasks 41–43 have already used it as four times. |

**`alternatives/` is the name the brief asked for explicitly**, and the name
matters. Not `legacy/`, which says superseded; not `contrib/`, which says
unofficial; not `vendor/`, which says someone else maintains it. These are real
implementations kept deliberately because a second implementation by a different
author is the strongest check available on arithmetic nobody has verified — and
Tasks 41, 42 and 43 each found something with one. Nothing is discarded.

## What does not move, and why

**`src/core/**`, `run.py`, `config/**`, `data/**`.** Nothing lands in them, so
`code_fingerprint` and `artifact_digests` do not change and **not one committed
ledger row goes stale.** Task 44's brief permits a fingerprint boundary; this
layout does not need one, because every role that needed a home was outside the
fingerprinted set. That is the strongest available outcome and it was a
constraint on the design, not a discovery afterwards.

**`agent-patterns/`.** Mentor-supplied, reviewed in Task 35, adopted as
conventions rather than as code. Not a name directory.

## Import surfaces after the move

The three trees use three different and mutually incompatible import styles
today. All three become explicit package paths rooted at the repo:

| tree | before | after |
|---|---|---|
| Gowtam | `from loop import paths`, `from schemas import ...` — bare top-level names requiring `Gowtam/L1` on `sys.path` | `from loop import paths`, `from loop.schemas import ...` |
| daksh | `from daksh.audit_agent.config import REPO_ROOT` | `from audit.config import REPO_ROOT` |
| nalin | `from nalin_l1.loop import paths` | `from alternatives.l1_engine.loop import paths` |

Gowtam's is the one that actually changes meaning. `schemas` and
`opencode_bridge` as **top-level package names** were flagged in Task 39: generic
enough that anything else claiming them would shadow or be shadowed, the same
hazard the two `src` packages had. Under `loop/` they are namespaced and the
hazard goes.

## Tests

One suite. `testpaths = ["tests"]` stays, and every component's tests move under
it:

```
tests/                 the harness's own
tests/audit/           tier 3's
tests/l1_engine/       the second engine's
```

`tests/l1_engine/` and not `tests/alternatives/`, which is the one line of this
document the execution had to correct. An `__init__.py` under
`tests/alternatives/` makes `tests.alternatives` a package, and with `tests/` on
`sys.path` the name `alternatives` then resolves to *that* directory instead of
the real one at the repo root -- so every `from alternatives.l1_engine import
...` fails. It is the same shadowing Task 39 found in the two packages both
named `src`, which is half of what this restructure exists to remove.

**No test is deleted and none is weakened.** Tests that cannot pass are skipped
with a reason naming what they need — daksh's eight need demo fixtures that exist
in no branch; nalin's seven need `pydantic`, which is not in the lockfile and
whose installation would move `environment_fingerprint` on every committed row.
A smaller number achieved by deletion is not the goal; an honest number is.

## What this layout does not solve

- **Two forked agentic loops.** Gowtam's goes to `loop/` because it drives this
  harness; nalin's stays inside `alternatives/l1_engine/loop/` because it drives
  that engine. They remain two forks of one program, which Task 41 left open
  deliberately.
- **The duplicated OpenCode agent definitions.** Byte-identical across both
  trees; deduplicating means choosing which tree owns them, which is the loop
  question again.
- **Paper retrieval**, named in Task 43's brief as loop-side work, **does not
  exist anywhere in the tree.** There is no file to place. When it is written it
  belongs in `loop/`, because it feeds prompt assembly.
