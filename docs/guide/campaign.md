# A campaign

One pre-registered question, a budget of N trials, and a gate at the end. A
campaign is driven by `scripts/nq_campaign.py` in five stages, each resumable,
each writing into `runs/campaigns/<id>/`.

[Documentation map](../README.md) · how-to: [setup/RUNNING-A-CAMPAIGN.md](../setup/RUNNING-A-CAMPAIGN.md) · rules: [design/pre-registration.md](../design/pre-registration.md)

## The stages

```
register  ->  run  ->  nulls  ->  report        (validate runs later, at a session's stop)
```

| Stage | What it does | Writes |
|---|---|---|
| `register` | Refuses anything it can check for free; preflights the proposer model; writes the question from `--question` if no file was given; freezes every setting and hashes the record | `campaign.json` (and `capacity-at-registration.json` with `--generation-width auto`) |
| `run` | N trials, in generations of `--generation-width`. Each: propose (see [proposer.md](proposer.md)), run (see [harness.md](harness.md)), check fidelity, maybe advance the champion | `ledger.jsonl`, `agent-calls.jsonl`, `probe-ledger.jsonl`, `sources/`, `fills/`, `champion.py`, `state.json`, `fidelity*.jsonl` |
| `nulls` | A null per scored candidate (see [scoring.md](scoring.md)) | `nulls.json` |
| `report` | The gate, the tier-2 summary, tier-3's deterministic checks, the committee (see [committee.md](committee.md)) | `result.json`, `summary.json`, `tier2-summary.json`, `tier3-verdict.json`, `committee-verdict.json` |
| `validate` | The winner, once, on the validation split. Run by the session at its stop, never mid-family | `validation.json`, `validation.jsonl`, `validation-null.json` |

```bash
python scripts/nq_campaign.py --stage register --name nq-me-01 --hypothesis config/hypotheses/q.md
python scripts/nq_campaign.py --stage register --name nq-me-01 --question "CVD divergence marks exhaustion"
python scripts/nq_campaign.py --stage run      --name nq-me-01
python scripts/nq_campaign.py --stage nulls    --name nq-me-01
python scripts/nq_campaign.py --stage report   --name nq-me-01
```

## What registration freezes

Everything the evaluation depends on goes into `campaign.json` and its digest,
which every later stage re-checks: the question's path and digest, its origin
(`hypothesis_origin`, for a question written from a statement), N, q, the
profile, the proposer model, the committee model (`--audit-model`), the null
source, the correction, the generation width, the probe and fidelity settings.
**Register-only flags are refused on later stages**, so nobody believes they
changed something they did not. There is **no default question**: register needs
exactly one of `--hypothesis` or `--question`.

## Where the time goes

Measured on the 10+ hour campaigns (2026-11-11):

| Campaign shape | Dominant cost |
|---|---|
| 400 trials, width 4, long replies (`nq-s07-*`) | Model calls 46-64% of wall time; 10-18% was replies cut off by the proxy at 300 s ([models.md](models.md)) |
| 400 trials, width 4 (`nq-orderflow-absorption-02`) | Backtests 84%; seven runaway strategies took ~8 h before the CPU limit killed them |
| 20 trials, width 1, fitted ML (`nq-ml-0*`) | Backtests 84-86%: ~2,000 s each, one at a time |

Each generation also waits for its slowest trial (12-33% idle at width 4).

## When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `register needs exactly one of --hypothesis … or --question` | No question given | Give one; the old silent default is gone |
| `campaign.json already exists` | A pre-registration is written once | Use a new `--name` |
| `--stage run` refuses: hypothesis digest changed | The question file was edited after registration | Restore it; a changed question is a new campaign |
| `--x applies to --stage register only` | A frozen setting was passed to a later stage | Drop the flag; the record has it |
| A capacity refusal at `run` | The machine cannot carry the registered width | `--ignore-capacity '<reason>'`, which is recorded |
| A stage died mid-way | Stages are resumable | Re-run the same stage; `run` resumes at the next trial |
