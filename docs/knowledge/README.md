# Knowledge library

A register of what this project has already learned, and a **frozen constraint
snapshot** the proposing agent may see.

The ledger and the campaign records remain the source of truth. This library
cites them. It does not become a second history.

## Two faces

| | Who reads it | When it changes |
|---|---|---|
| **Store** (`log.jsonl`, `closed.md`, `open.md`, `harness.md`) | Humans, `scripts/knowledge_check.py`, `scripts/rank_papers.py` (closed families via the dataset doc) | After `--stage report`, never during `run` |
| **Snapshot** (`runs/campaigns/<id>/knowledge.md`) | The proposing agent, identically on every trial of that campaign | Compiled at `--stage register`, then frozen |

The proposer never queries `log.jsonl`. `scripts/knowledge_compile.py` is
deterministic: closed families and harness *rules*, no near-misses, no scores,
no parameters, no date windows. The compiled bytes are hashed into
`campaign.json` next to `hypothesis_digest`. Editing the store after register
does not change an in-flight campaign, because that campaign reads its own copy.

The snapshot lives in the campaign directory, not in `ARTIFACT_GLOBS`. A global
file under `config/` would move the artifact fingerprint of every trial, and
registering campaign B while A is running would invalidate A's next trial.

## What may be in the snapshot

- **Do not re-explore** — `closed_family` rows for this dataset.
- **Do not do this** — `harness_lesson` rows that carry a `rule_for_agent`.

Trial-to-trial adaptation stays on the history whitelist in `LLMAgent`: score,
trades, sessions, fill-log identity. That is priced by the campaign's N.

## What may not

Scores, p-values, N, parameters (`20/50`), calendar windows ("March to June"),
validation, the holdout, or "what worked" from a training-only winner. Those
stay in the store, next to `how_it_died`. `scripts/knowledge_check.py` refuses
them in any field the compiler would emit.

## Write protocol

1. Campaign finishes `--stage report`.
2. Draft at most three rows: family-level, near-miss if the champion looked
   good, harness lesson if the search taught you something about the metric.
3. `how_it_died` is required on `near_miss`. A positive-looking claim without
   it fails the check.
4. `python scripts/knowledge_check.py`
5. Commit on a human branch. Stage by name.

The research agent cannot write here. Entries are human (economics auditor as
drafter). `visible_to_agent` on a store row is always false; the compiler
decides what the snapshot contains.

Canonical closed families for NQ stay in `docs/datasets/nq-es-qqq.md`.
`closed.md` cites that section and does not fork it.
