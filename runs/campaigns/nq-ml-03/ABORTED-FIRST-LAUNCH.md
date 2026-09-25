# First launch aborted before any trial ran — 2026-09-25

**No trial ran, no ledger row was written, and N is untouched.** The launch
environment did not carry two of the four R2 variables: `secrets/r2.env` holds
plain `KEY=value` lines with no `export`, so `source secrets/r2.env` set them as
shell variables and the `run.py` subprocess never saw them. `R2_ACCESS_KEY_ID`
and `R2_SECRET_ACCESS_KEY` happened to be in the environment already, which is
why the failure looked partial rather than obvious.

`run.py` refused at startup with

```
the NQ adapter needs read-only bucket credentials in the environment;
missing R2_ENDPOINT, R2_BUCKET. See docs/setup/R2_ACCESS.md.
```

and exited before minting a ledger row, so `run_trial` reported
`harness_failed: run.py produced no ledger row` for each of the first five
proposals. **`harness_failed` is not a trial** (pre-registration §3: the
candidate never ran, and charging it to N charges the agent for our breakage),
so the campaign resumes at trial 0 with the full budget of 20 intact.

**What it did cost:** five proposal calls to the model, recorded in
`agent-calls-aborted.jsonl`. That file is kept and renamed rather than deleted —
the calls happened — and the live campaign writes a fresh `agent-calls.jsonl`
whose trial indices start again at 0.

**The fix at the launch site:** `set -a; source secrets/r2.env; set +a`, or run
inside the direnv-loaded shell, which is what `.envrc`'s `dotenv_if_exists`
does correctly.
