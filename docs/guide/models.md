# Models and the gateway

Every model call in the loop goes through one LiteLLM gateway. This guide is
which part calls which model, how a call can fail, and what to do about it.
The measurements behind it are in [setup/MODEL-ACCESS.md](../setup/MODEL-ACCESS.md).

[Documentation map](../README.md) · running against your own key: [setup/LOCAL_PROXY.md](../setup/LOCAL_PROXY.md)

## Files

| File | Role |
|---|---|
| `src/core/loop/llm.py` | The proposer's client: `preflight`, `_one_attempt`, retries, `NO_CACHE`, `cached_prompt_tokens` |
| `audit/committee/llm.py` | The second client, used by the committee, the writer, the refiner and the review seats |
| `.envrc` / environment | `LITELLM_BASE_URL`, `LITELLM_API_KEY` |

## Which part calls which model

Every part can be set separately. Options marked *register* are frozen into
`campaign.json`; the plan key does the same from a session plan.

| Part | Default | Option | Plan key |
|---|---|---|---|
| Proposer | `deepseek-v4.1-flash` | `--model` (register) | `model` |
| Question writer (`--question`) | `mimo-v2.6-pro` | `--writer-model` (register) | `writer_model` |
| Writer's paper picker | `gemini-2.5-pro` | `--picker-model` (register) | `picker_model` |
| Writer's review seat | `mimo-v2.6-pro` | `--reviewer-model` (register) | `reviewer_model` |
| Fidelity judge | `gemini-2.5-pro` | `--judge-model` (register) | `judge_model` |
| Committee, all four auditors | `mimo-v2.6-pro` | `--audit-model` (register) | `audit_model` |
| One auditor | as `--audit-model` | `--auditor-model AXIS=MODEL`, repeatable (register) | `auditor_models: {leakage: …}` |
| Refiner | `mimo-v2.6-pro` | `refine_hypothesis.py --model` | `refine_model` |
| Refiner's two seats | `mimo-v2.6-pro` | `refine_hypothesis.py --reviewer-model` | `refine_reviewer_model` |
| Diagnosis and paper vetting | the refiner's model | `refine_hypothesis.py --retrieval-model` | `retrieval_model` |
| Screening and ranking new papers | `gemini-2.5-pro` | `refine_hypothesis.py --paper-model`; `screen_papers.py` / `rank_papers.py --model`; `retrieve.py --screen-model --rank-model` | `paper_model` |
| Paper distiller | `mimo-v2.6-pro` | `distil_paper.py --model`, `retrieve.py --model` | — |

**As of 2026-11-12 (afternoon):** `mimo-v2.6-pro`, `mimo-v2.6-flash`,
`gemini-3.1-pro-preview` and `gemini-2.5-pro` serve themselves. **Both DeepSeek
models fall back to Gemini for our key**, at every request shape (even a bare
one-line request), so the proposer default refuses until the gateway's DeepSeek
deployment works for us; until then pass `--model gemini-3.1-pro-preview`. The
default is `deepseek-v4.1-flash` on Brian's advice (smarter, faster and cheaper
than v4-pro), not yet measured here.

## How a call can fail

| Failure | What it means | What the loop does |
|---|---|---|
| `ModelSubstituted` | Another model answered | Refuses. Running on, the record would name a model that did not answer |
| HTTP 502 at ~300 s | The Apache proxy timed out a long reply | Resends after 30, 120, 300, 600, 600 s (5 tries); ~72% of resends succeed |
| HTTP 429 / other 5xx | Rate limit or upstream fault | Same retries |
| HTTP 400 / 401 / 422 | Our request is wrong, or the key's budget is spent | No retry |
| `finish_reason: length` | Ran out of output tokens (often in reasoning) | Recorded; the proposal is blocked or repaired |
| `ReadTimeout` | No reply within `request_timeout_s` (600 s) | Retried as transport |

## What makes calls slow

Latency is about **3 s + 11 s per 1,000 output tokens**; prompt length barely
matters, so **caching saves money, not time**. Long replies are the cost, and
beyond ~28,000 output tokens a reply hits the proxy's 300 s limit. Each call's
`latency_s`, tokens and `cached_tokens` are in `agent-calls.jsonl`.

## When it goes wrong

| Symptom | Fix |
|---|---|
| `ModelSubstituted: asked 'X', served 'Y'` | Pick a model that serves itself (check with `preflight`); tell the gateway admin |
| Many 502s at 300 s | Ask the admin to raise Apache `ProxyTimeout`; or shorten replies |
| `HTTP 422` | The key's budget is exhausted; check `x-litellm-key-spend` in a response's headers |
| Writer or seat "did not run" | Its model is substituted; set its flag |
| Cache rate shows 0% | Through the deepseek fallback it is 0; on gemini directly ~80% after the first call |
| A model is "on the opencode models list" but substituted | That list is the gateway's catalogue (`litellm/…`), not a test of serving; only a preflight shows what answers |
