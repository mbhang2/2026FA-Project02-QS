# The proposer

The language model that writes strategies. Within one campaign it reads the
question, the files it may edit and the history of earlier trials, and returns
one complete strategy file per trial. It sees scores; it never sees the gate,
the p-values, or how many trials the correction will charge for.

[Documentation map](../README.md) · deep design: [design/loop.md](../design/loop.md)

## Files

| File | Role |
|---|---|
| `src/core/loop/llm.py` | `LLMAgent`: builds the prompt, calls the model, runs every check and repair, records each call |
| `config/prompts/strategy-agent.md` | The prompt template. Fingerprinted: a campaign records its digest |
| `docs/agent/strategy-api.md`, `docs/agent/data.md`, `docs/design/context-api.md` | Pasted into the prompt verbatim: the API the strategy writes against |
| `src/strategies/current.py` | The file a trial runs. Its committed version is the baseline a campaign starts from (`BASELINE_PATH`); each trial overwrites it and restores it afterwards |
| `src/core/checks/` | The static check: `compile()`, an AST pass, mypy against the Strategy protocol |
| `src/core/loop/probe.py` | The synthetic probe (Amendment 7) |
| `src/core/loop/fidelity.py`, `src/core/loop/perturb.py` | Does the code do what the question fixes? (Amendment 14) |
| `scripts/fidelity_judge.py`, `config/prompts/fidelity-judge.md` | A model reads the code against the question's judged requirements |
| `scripts/nq_campaign.py` (`agent_for`, `stage_run`) | Builds the agent from the campaign record and drives it |

## One proposal, in order

1. **Choose the file to edit.** Usually the champion (the best trial so far);
   with `explore_epsilon > 0`, sometimes an earlier trial's source instead. The
   agent is not told which.
2. **Build the prompt** from the template: the API documents, the run facts
   (dataset, fill model, commission, caps), the hypothesis, the knowledge
   snapshot, the file to edit, and the history. The history is a whitelist of
   fields per trial — outcome, score, trade count, "identical trades to #k" —
   never a p-value or verdict.
3. **Call the model** through the gateway (`LITELLM_BASE_URL`). Every call
   sends `cache: {"no-cache": true}` so the gateway never replays an earlier
   reply. A reply from a different model than the one asked for raises
   `ModelSubstituted` and stops the campaign. HTTP 429 and 5xx are retried up to
   5 times after 30, 120, 300, 600, 600 s, **except a 502 at the proxy's 300 s
   limit**: a proposal is retried once at once, an optional rewrite not at all,
   and a rewrite kind that hits it 3 times is skipped for the rest of the
   campaign (`PROXY_TIMEOUT_S` in `llm.py`).
4. **Static check.** `compile()`, an AST pass and mypy, on a scratch copy;
   nothing executes. A failure goes back to the model as a `repair`, at most
   twice (`max_repair_attempts`).
5. **Fidelity checks**, only when the question has a constraints file. The code
   is checked mechanically, then its behaviour on synthetic data (`perturb`);
   violations go back as `fidelity_repair`. Then the judge reads the code against
   the `judged:` requirements; under `fidelity_judge: repair`, a violation quoting
   a code line goes back as `judge_repair`. At most two each; none is a trial.
6. **Probe.** The file runs once under a synthetic profile. If it crashes, or
   trades identically to the file it was edited from, the result goes back as a
   `probe_repair` (at most `max_probe_repairs = 2`). **The probe never blocks**:
   the last statically clean file always goes to the trial.
7. **The trial** (see [harness.md](harness.md)) scores the file. If it beats the
   champion strictly, it becomes the new champion.

If step 4 still fails after its repairs, the proposal is **blocked**: the trial
is recorded as crashed (`static_check_unrepaired`) and **still counts toward N**,
because the look happened. All of steps 3-6 share one deadline,
`proposal_wall_clock_s = 600`.

## What it records

`runs/campaigns/<id>/agent-calls.jsonl`, one line per trial, one entry per call:
`kind` (`propose`, `repair`, `probe`, `probe_repair`, `fidelity_repair`,
`judge_repair`, `blocked`), `ok`, `latency_s` (the HTTP round trip only),
`prompt_tokens`, `cached_tokens`, `completion_tokens`, `reasoning_tokens`,
`model_served`, `finish_reason`, `error`. This is where to look first for any
proposer problem.

## Settings that matter

Frozen into `campaign.json` at registration; changing them later is refused.

| Setting | Where | Default |
|---|---|---|
| Model | `--model` at register | `deepseek-v4.1-flash` (since 2026-11-12) |
| Output budget | `PROPOSER_MAX_TOKENS` in `nq_campaign.py` | 64,000 (covers reasoning) |
| Probe on/off | `--probe` / `--no-probe`, `probe_before_trial` in `config/run.yaml` | on |
| Fidelity | `fidelity_policy`, `fidelity_judge` in `config/run.yaml` | `record`, `repair` |
| Parallelism | `--generation-width` (Amendment 12) | 1 |

## When it goes wrong

| Symptom in `agent-calls.jsonl` or the ledger | Cause | Fix |
|---|---|---|
| Every proposal `blocked`, error "mypy is not installed" | No mypy in the campaign's Python | `pip install -r requirements.lock`; see [data-and-environment.md](data-and-environment.md) |
| `blocked: static_check_unrepaired` with a real type error | The model keeps writing code that fails the protocol | Read the error; often a seed or champion that itself fails the check |
| `no python block in the reply (finish_reason=length …)` | The reply ran out of tokens, usually in reasoning | Raise the output budget, or use a model that reasons less |
| Many `probe_repair` calls per trial | Proposals crash or do nothing on synthetic data | Check the probe's error text; the probe profile may lack a feed the question needs |
| `HTTP 502` at ~300 s | The proxy timed out a long reply | [models.md](models.md) |
| `ModelSubstituted` | The gateway served another model | [models.md](models.md) |
| Trials flagged off question | The code does not do what the question fixes | The constraints file, and [hypotheses.md](hypotheses.md) |
| Calls slow (one to two minutes) | Long replies: latency is about 11 s per 1,000 output tokens | Ask for shorter output (a diff, fewer repairs); prompt length barely matters |
