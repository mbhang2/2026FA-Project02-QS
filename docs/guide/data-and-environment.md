# Data and environment

What market data a run reads, how the splits are kept apart, and what a
machine needs to run campaigns.

[Documentation map](../README.md) · deep: [agent/data.md](../agent/data.md) (what a strategy sees), [datasets/nq-es-qqq.md](../datasets/nq-es-qqq.md) (the adapter), [data-inventory.md](../data-inventory.md) (the bucket)

## Data

| Dataset | Adapter | Used for |
|---|---|---|
| NQ futures trades | `nq-trades:NQ` (`src/core/data/nq.py`) | Every real campaign (profiles `nq-train`, `nq-train-fitted`) |
| SPX options | `src/core/data/options_spx.py` | `spx-*` profiles |
| Synthetic | `src/core/data/synthetic.py` | Rehearsals, the probe, positive controls |

- **Sessions, not files.** A CME session runs 17:00 CT to 16:00 CT the next day;
  N counts sessions. 564 NQ sessions are assemblable (2024-07-04 to 2026-09-09).
- **The split is pre-registered** in `config/calendars/nq-trades.yaml`: train,
  validation, holdout. A profile names one split; nothing else chooses it.
  Train has 384 scored sessions, validation 86, the holdout 73.
- **The holdout is locked.** Serving it raises `HoldoutLocked` unless unlocked
  once, with a reason that lands in the run record (`scripts/holdout.py`).
- **Where the data lives.** The Cloudflare R2 bucket `msfe-practicum`. Ask for a
  personal read-only key (`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY`) and follow [setup/R2_ACCESS.md](../setup/R2_ACCESS.md).
  `data/manifest.json` fingerprints the bucket.
- Licensed market data is **not** in this repository (see
  [PUBLIC_RELEASE.md](../PUBLIC_RELEASE.md)); tests that need it skip without it.

## Environment

```bash
pip install -r requirements.lock      # exact pins, including mypy
direnv allow                          # .envrc: gateway and R2 variables
pytest -q                             # the whole suite passes on a complete environment
```

| Need | Why | Check |
|---|---|---|
| **mypy** (`requirements.lock`: 2.3.1) | The proposer's static check runs it; without it **every proposal is blocked** | `python3 -c "import mypy"` |
| `LITELLM_BASE_URL`, `LITELLM_API_KEY` | Every model call | see [models.md](models.md) |
| R2 variables | Real market data | [setup/R2_ACCESS.md](../setup/R2_ACCESS.md) |
| Linux, for scored campaigns | Memory limits bind only on Linux; macOS isolation is best-effort | [setup/LINUX_RUNNER.md](../setup/LINUX_RUNNER.md) |

**Install into a fresh virtual environment.** Installing single pins from
`requirements.lock` into a Python that does not match it can break it. On
2026-11-12, adding the lock's `pandas_market_calendars` upgraded numpy to 2.4.6
under pandas 2.1.1, and pandas stopped importing. It was restored with
`numpy==1.26.4`, but that Python still differs from the lock (which wants
pandas 3.0.5).

**Never commit a credential.** `secrets/` is ignored; stage files by name.

## When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Every proposal `blocked`, "mypy is not installed" | No mypy | `pip install -r requirements.lock` |
| `ValueError: numpy.dtype size changed` on `import pandas` | numpy upgraded under an older pandas | A matching pair (numpy 1.26.x for pandas 2.1), or a fresh venv from the lock |
| `HoldoutLocked` | The holdout was asked for without an unlock | Intended; see [sessions.md](sessions.md) |
| Live-data tests skipped | No R2 variables | Expected offline |
| `pip check` reports conflicts | Partial installs | A fresh venv from `requirements.lock` |
