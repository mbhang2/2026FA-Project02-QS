# Which models we can actually use

**Measured 2026-10-03 against the gateway, not read off a list.** Every number is from a real call at our real prompt size.

The headline: **the frontier models are not available to us.** Of the 88 models the gateway lists, **28 return HTTP 403** and **19 silently answer as a different model**. What is left for the proposer is a short list.

## 0. Re-measured 2026-10-22. The gateway changed; some of section 1-4 no longer holds

Everything below this section is the 2026-10-03 sweep and is kept because it is
what the decisions of that week were made on. The gateway went 503, came back,
and behaves differently now. Where the two disagree, **this section is the
current reading**.

**`claude-haiku-4-5` ran 400 real trials with zero substitutions.**
`nq-orderflow-absorption-02`, 2026-10-21/22, prompts averaging 145,415 input
tokens -- well past the 167,489 characters (~42k tokens) at which section 1
says haiku is substituted. So the claim "haiku cannot be the proposer" no
longer holds as written, and **the substitution trigger has moved from prompt
size to `max_tokens`**.

**The trigger is now the output ceiling, and haiku's is 64,000.** Bisected on
the live endpoint:

    49,152 -> claude-haiku-4-5        64,000 -> claude-haiku-4-5
    63,488 -> claude-haiku-4-5        64,256 -> gemini-3.1-pro-preview
    65,536 -> gemini-3.1-pro-preview  65,537 -> HTTP 400 from Vertex AI

The 400 at 65,537 is the proof it had routed to Google. `PROPOSER_MAX_TOKENS`
was 65,536, so every `nq_campaign` trial on haiku raised `ModelSubstituted` on
trial 0 until it was lowered. **`preflight` did not catch it** because it
probed at a fixed 512 and the substitution depends on the request shape; it now
probes at the agent's real `max_tokens`.

**There is a response cache in front of the gateway.** Four identical requests
at `temperature=0.7` returned ONE distinct answer -- the first in 2.04 s, the
rest in 0.18-0.25 s. With `cache: {"no-cache": true}` in the body, four of four
differed at ~0.97 s. The **body parameter works and the
`x-litellm-no-cache` header does not**. Every call in both clients now sends
it; see `NO_CACHE` in `src/core/loop/llm.py` and `audit/committee/llm.py` for
why this is a correctness issue and not a performance one.

**Authorised, re-checked at 64,000 output tokens.** Only four answer as
themselves: `deepseek-v4-pro`, `minimax-m3`, `step-5-preview`,
`gemini-3.1-pro-preview`. `deepseek-v4-pro` and `minimax-m3` were both verified
honest up to **128,000**.

**Still forbidden (403):** the whole Claude line above haiku -- `opus-5`,
`opus-5-5`, `opus-4-8`, `sonnet-5`, `sonnet-5-5`, `fable-5-1` -- plus
`gpt-6-sol`, `gpt-6.1-sol`, `gpt-6-astra`, `kimi-k3`, `qwen3.7-max`,
`qwen3.8-max`, `glm-5.3`. This is a permissions setting on our key, not a
capability limit: **worth asking Brian to unlock one**, since any of them would
likely beat the authorised set.

**Newly substituted, and one of them is a trap:**

    claude-haiku-5-5 -> gemini-2.5-flash
    grok-4.7         -> gemini-3.1-pro-preview
    gemini-4-argon   -> gemini-3.1-pro-preview
    longcat-2.0      -> gemini-3.1-pro-preview

`claude-haiku-5-5` reads like a free upgrade from `claude-haiku-4-5` and is
served by a Gemini flash model.

**Do not register `gemini-3.1-pro-preview` as the proposer** even though it is
honest. It is the wildcard fallback target, so a substitution INTO it is
undetectable -- asked gemini, served gemini -- which gives up the guard that
caught every case above.

**`deepseek-v4-pro` is no longer a 10-second model.** Section 2 has it at 10.2 s
and 3,507 reply characters. Measured 2026-10-22 it emits **up to 88,799
completion tokens on one call, 76,337 of them reasoning**, and a rehearsal
generation of four took 7-10 minutes against `claude-haiku-4-5`'s one. Size
`max_tokens` from that, not from section 2: 16,384 truncates it on every call.

**The admin endpoints are 403 for our key** -- `/key/info`, `/user/info`,
`/spend/logs`, `/model/info`. So remaining budget and per-token prices cannot
be read from here, and a mid-run budget refusal cannot be predicted. Only Brian
can see them.

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
| committee, four auditors | `mimo-v2.6-pro` | **changed 2026-10-04 from the campaign's own proposer model** -- see below. Four seats once per campaign is a cheap place to spend a slow model, unlike the proposer, which calls twenty times |

### The committee was not independent. Decided and fixed 2026-10-04

`scripts/nq_campaign.py` passed `model=(record.agent_config or {}).get("model", MODEL)` into `review()`, so the four auditors ran on **whatever model wrote the strategy they were auditing**. The last nine campaigns had `deepseek-v4-pro` auditing `deepseek-v4-pro`; the eight before that had `claude-haiku-4-5` auditing `claude-haiku-4-5`.

The committee is the only tier that has ever vetoed anything. An auditor that is the same model as the author is not an independent check of it.

**Michael decided it on 2026-10-04.** `review()` now defaults to `AUDIT_MODEL = "mimo-v2.6-pro"` and the campaign passes `proposer_model=` instead, which is recorded rather than used to pick the auditor.

**What it means for the 17 verdicts already on disk, stated plainly: none of them was an independent check, and nothing retroactively makes them one.** They are not being rewritten. Two things limit the damage. Every one of the 17 is a `VETO`, so independence has not yet changed an outcome -- it would have mattered the first time something passed. And the committee cannot approve in any case (`can_approve: False`), so a verdict was never evidence *for* a strategy, only against one.

**It is now detectable rather than only fixed.** `committee-verdict.json` carries `proposer_model`, and `independent` as a **tri-state**: `false` is the defect, `null` means the record does not say, and the two are kept apart so an unknown cannot read as a pass. Files written before this change have neither key, which is itself the marker.

## 6. Reproducing this

Two sweeps: `/models`, then `/chat/completions` at 8 tokens to classify authorised / forbidden / substituted, then the authorised ones at the real proposal prompt built by `nq_campaign.agent_for(...)._prompt([], repair="")`.

**Run it from one process.** Two concurrent probes writing one results file produced a merged JSON with mismatched latencies and reply sizes on the first attempt here; the table above is rebuilt from a single completed run's log.

None of this access is announced, so re-measure whenever something stops working.
