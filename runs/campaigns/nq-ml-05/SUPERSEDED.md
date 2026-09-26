# nq-ml-05 stopped at 10 of 20 — our token budget, not the model

**Stopped deliberately 2026-09-26, by Michael, after the blocked-proposal rate
hit 4 of 10.** Ten trials ran; nothing is deleted.

## What went wrong

Four proposals came back as `no python block in the reply`, at 7,805, 1,974,
1,928 and 1,865 characters. The cause is **our `max_tokens`, not the model and
not the data**:

| a successful call, measured | tokens |
|---|---|
| reasoning | 12,004 |
| visible text | 2,990 |
| **total completion** | **14,994** |
| budget it was given | 16,384 |

`gemini-3.1-pro-preview` spends ~12,000 tokens thinking before it emits a
visible character. A successful call used **91% of the budget**, leaving ~1,400
tokens of headroom, so any call whose reasoning ran 10% long truncated before
the code fence closed. The reply then has no fenced block, `extract_source`
returns `None`, and the pre-registered rule turns that into a blocked proposal:
a crashed trial that counts toward N.

**The accounting was right and the experiment was not.** 4 of 10 trials bought
nothing but a truncated reply, so the gate would have been asked to judge a
search with about 12 real looks instead of 20.

## Third instance of one defect

1. `audit/committee/llm.py` at `max_tokens=4096` against deepseek's ~7k of
   `reasoning_content` — three of `nq-ml-02`'s four committee seats returned
   empty and the campaign's verdict rested on one seat.
2. `LLMAgent.preflight` at `max_tokens=16` — **no reasoning model could be
   registered at all**, and the gateway reported the empty output as
   `ContentPolicyViolationError`, which reads like a refusal.
3. This: the proposer at 16,384.

Each time the visible symptom pointed somewhere else — an empty verdict, a
content-policy block, a model that "won't write code". The committee client was
fixed in place after (1) and the lesson was not carried to the other two call
sites.

## Why it was invisible

`Call` recorded neither `finish_reason` nor `reasoning_tokens`, so
`agent-calls.jsonl` said only "no python block in a 1865-character reply" —
the same message whether the model refused, rambled or ran out of budget. The
committee client has recorded both since nq-ml-02. Diagnosing this needed a
call reproduced by hand against the live endpoint.

## Fixed before nq-ml-06

- `PROPOSER_MAX_TOKENS = 65_536`, a named constant used by both the agent and
  the preflight, ~4x the largest reasoning budget measured.
- `Call` records `finish_reason` and `reasoning_tokens`; the no-block error
  names all four numbers; `drain_log` exposes `truncated` and
  `reasoning_tokens` per trial.

## The ten trials

| # | result | trades |
|---|---|---|
| 0 | **rejected** | 482 (exposed in 43 of 100) |
| 1 | −0.3099 | 4,767 |
| 2 | −0.1368 | 8,861 |
| 3 | −0.1414 | 9,408 |
| 4 | −0.0377 | 1,048 |
| 5 | **blocked** | — |
| 6 | **blocked** | — |
| 7 | **rejected** | 96 |
| 8 | **blocked** | — |
| 9 | **blocked** | — |

Worth one observation, since it is the only thing here that is about research
rather than plumbing: **the proposer changes the search visibly.** gemini went
straight to 4,767 → 8,861 → 9,408 trades, an order of magnitude above anything
deepseek proposed across two campaigns (max ~3,400), then pulled back to 1,048
for its best score. Same seed, same hypothesis, same floor.

**Not poolable with anything.** Superseded for an infrastructure defect, not
because the numbers were disappointing — every scored trial here was negative,
and so were the first ten of `nq-ml-03`, which went on to produce the
project's best train score.
