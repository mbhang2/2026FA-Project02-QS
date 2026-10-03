# Which models we can actually use

**Measured 2026-10-03 against the gateway, not read off a list.** Every number is from a real call at our real prompt size.

The headline: **the frontier models are not available to us.** Of the 88 models the gateway lists, **28 return HTTP 403** and **19 silently answer as a different model**. What is left for the proposer is a short list.

## 1. The only test that counts is the big one

A model that answers a two-word prompt may still refuse ours. `claude-haiku-4-5` passes a small prompt and is **substituted to `gemini-3.1-pro-preview`** at our real proposal prompt of **167,489 characters** (~42k tokens). Every row below was tested at that size.

`ModelSubstituted` catches the swap and refuses the trial, so no record has ever been corrupted by it -- but it is why haiku cannot be the proposer however good it might be.

## 2. Usable as a proposer, fastest first

| model | latency at 167k | reply chars | note |
|---|---|---|---|
| `glm-5.3-flash` | 0.3 s | 8,219 | **suspect - under a second for a 42k-token prompt** |
| `hy3` | 0.3 s | 3,820 | **suspect - under a second for a 42k-token prompt** |
| `minimax-m2-maas` | 0.3 s | 3,875 | **suspect - under a second for a 42k-token prompt** |
| `mimo-v2.5-pro` | 0.4 s | 4,048 | **suspect - under a second for a 42k-token prompt** |
| `deepseek-v4-pro` | 10.2 s | 3,507 |  |
| `gemini-3.7-flash` | 19.9 s | 5,644 |  |
| `gemini-2.5-flash` | 20.5 s | 4,184 |  |
| `gemini-3.6-flash` | 27.6 s | 4,489 |  |
| `gemini-3.5-flash` | 38.2 s | 4,248 |  |
| `gemini-2.5-pro` | 38.9 s | 4,219 |  |
| `gemini-3-flash-preview` | 45.6 s | 3,868 |  |
| `gemini-3.1-pro-preview` | 80.8 s | 2,920 |  |
| `longcat-2.0` | 159.7 s | 2,605 | dominates the 225 s backtest |
| `minimax-m3` | 216.3 s | 2,737 | dominates the 225 s backtest |
| `mimo-v2.5` | 284.9 s | 13,121 | dominates the 225 s backtest |
| `qwen3.8-flash` | 315.6 s | 8,709 | dominates the 225 s backtest |
| `mimo-v2.6-flash` | 315.7 s | 8,717 | dominates the 225 s backtest |
| `hy4-preview` | 315.8 s | 7,013 | dominates the 225 s backtest |
| `deepseek-v4.1-flash` | 676.2 s | 8,096 | dominates the 225 s backtest |
| `mimo-v2.6-pro` | 677.1 s | 7,906 | dominates the 225 s backtest |

**A trial's backtest is 225 s.** Anything past ~100 s stops hiding behind it and starts setting the pace. The practical set is **`deepseek-v4-pro` (10 s)**, **`gemini-2.5-pro` (39 s)** and **`gemini-3.1-pro-preview` (81 s)**.

**Four models replied in under a second** -- `glm-5.3-flash`, `hy3`, `minimax-m2-maas`, `mimo-v2.5-pro`. That is far too fast to have read 42k tokens: cached, truncating, or ignoring the input. Not used, and not trusted without a check.

Also at full size: `auto-router` -> `gemini-3.1-pro-preview`, `claude-haiku-4-5` -> `gemini-3.1-pro-preview`, `deepseek-r1-0528-maas` -> `gemini-3.1-pro-preview` substitute, and `deepseek-v4-flash` returns an empty reply with `finish_reason=length` -- the reasoning-budget failure we have now hit six times elsewhere.

## 3. Forbidden -- HTTP 403 (28 of 88)

- `claude-fable-5-1`
- `claude-opus-4-5`
- `claude-opus-4-6`
- `claude-opus-4-8`
- `claude-opus-5`
- `claude-opus-5-5`
- `claude-sonnet-4-5`
- `claude-sonnet-4-6`
- `claude-sonnet-5`
- `claude-sonnet-5-5`
- `copilot-fallback`
- `glm-4.7-maas`
- `glm-5-maas`
- `glm-5.2`
- `glm-5.3`
- `gpt-5-mini (gemini-2.5-flash-lite)`
- `gpt-5.4-nano`
- `gpt-5.6-terra`
- `gpt-6-astra`
- `gpt-6-luna`
- `gpt-6-sol`
- `gpt-6.1-sol`
- `kimi-k2-thinking-maas`
- `kimi-k2.7-code`
- `kimi-k3`
- `qwen3.6-plus`
- `qwen3.7-plus`
- `qwen3.8-max`

**This is the ask.** Every Claude model except haiku is on that list, including `claude-opus-5` and `claude-sonnet-5-5`, and so is the entire `gpt-6` tier including **`gpt-6-luna`** and `gpt-6.1-sol`. Access to even one of them is the cheapest quality upgrade available to this project -- cheaper than anything we can write.

## 4. Silently substituted (19 of 88, at a small prompt)

HTTP 200, a different model in the payload. The guard catches it; the point is that the model you ask for is not always the model you get.

- `Gemma-4-26B-A4B-it-MTP-GGUF:latest` -> `gemini-3.1-pro-preview`
- `big-pickle` -> `gemini-3.1-pro-preview`
- `deepseek-flash` -> `gemini-3.1-pro-preview`
- `gemini-3.8-flash` -> `gemini-3.1-pro-preview`
- `gpt-5.6-luna` -> `gemini-3.1-pro-preview`
- `grok-4.6` -> `gemini-3.1-pro-preview`
- `grok-4.7` -> `gemini-3.1-pro-preview`
- `jev-1.13` -> `gemini-3.1-pro-preview`
- `jev-1.13-free` -> `gemini-3.1-pro-preview`
- `mimo-v2.5-free` -> `gemini-3.1-pro-preview`
- `mimo-v2.6-flash-free` -> `gemini-3.1-pro-preview`
- `minimax-m2.7` -> `gemini-3.1-pro-preview`
- ... and 7 more

## 5. What each part of the loop runs on, after 2026-10-03

| stage | model | why |
|---|---|---|
| proposer | `deepseek-v4-pro` | the only strong model serving itself at 167k in reasonable time |
| paper screen / rank / distil | `gemini-2.5-pro` | **changed today from `claude-haiku-4-5`** -- the weakest authorised model was doing a judgement task. A different family from the proposer on purpose: the model that judges a paper should not be the model that writes the code |
| hypothesis refiner | `deepseek-v4-pro` | |
| committee, four auditors | **the campaign's own proposer model** | **a defect -- see below** |

### The committee is not independent, and that is a real problem

`scripts/nq_campaign.py` passes `model=(record.agent_config or {}).get("model", MODEL)` into `review()`, so the four auditors run on **whatever model wrote the strategy they are auditing**. Every recent campaign has had `deepseek-v4-pro` auditing `deepseek-v4-pro`.

The committee is the only tier that has ever vetoed anything. An auditor that is the same model as the author is not an independent check of it. Fixing it changes what every past verdict means, so it is a decision and not a patch -- recorded here rather than quietly changed.

## 6. Reproducing this

Two sweeps: `/models`, then `/chat/completions` at 8 tokens to classify authorised / forbidden / substituted, then the authorised ones at the real proposal prompt built by `nq_campaign.agent_for(...)._prompt([], repair="")`.

**Run it from one process.** Two concurrent probes writing one results file produced a merged JSON with mismatched latencies and reply sizes on the first attempt here; the table above is rebuilt from a single completed run's log.

None of this access is announced, so re-measure whenever something stops working.
