# nq-ml-04 — running notes

Same design as `nq-ml-03` (nq-train-fitted, N = 20, `deepseek-v4-pro`, exposure
floor 0.70, probe off) from a **corrected** ViperQ seed — digest
`8fc28c884db7d7c7`, against `nq-ml-03`'s `f647019d179b1335`. The correction and
what is owed for it are in `docs/design/pre-registration.md`.

## Interrupted at trial 6 by a model substitution — 2026-09-25

The driver stopped itself:

```
ModelSubstituted: asked the endpoint for 'deepseek-v4-pro' and it answered with
'gemini-3.1-pro-preview'.
```

**No row is contaminated, and the campaign is resumable.** `ModelSubstituted` is
raised inside `_one`, before the reply is parsed and long before `run_trial`
writes `current.py`, so the seventh proposal produced no ledger row and no file
was left behind (`git status` on `src/strategies/current.py` is clean). All six
completed trials record `models_served: ['deepseek-v4-pro']` in
`agent-calls.jsonl`. The guard fired *before* the record could become false,
which is what it is for — so the exception's own advice to "discard whatever has
run" does not apply here. It is written for the case where trials already ran on
the wrong model.

This is Task 37's defect recurring: `claude-haiku-4-5` started returning 503 and
the gateway answered 200 with `gemini-3.1-pro-preview`. Same substitute model,
two years of the same behaviour.

**A minute later the route was gone entirely** — `HTTP 404, Cannot find any
route matching` — while `deepseek-v4-pro` is *still listed* by the gateway's
`/models`. Listed but unroutable is a gateway-side misconfiguration in progress,
not a withdrawn model, so the campaign waits rather than re-registering.

### The gateway aliases models in the open, and the listing says so

Worth passing to Brian. Three entries in `/models` name their substitute in
the id itself:

```
gpt-5-mini (gemini-2.5-flash-lite)
gpt-5.6-luna (gemini-2.5-flash-lite)
gpt-5.6-terra (gemini-3.1-pro-preview)
```

`gpt-5.6-luna` also appears *unqualified*. So the same name can resolve two
ways, and only the `model` field on the response says which answered. **This is
why `_same_model` checks every reply rather than trusting registration**, and it
is the concrete argument for that check: a campaign that recorded `gpt-5.6-luna`
could have run on either.

### Resume rule

Wait for `deepseek-v4-pro` to serve itself, then
`--stage run --name nq-ml-04`, which picks up at trial 6 with the budget intact.
**Do not swap the model**: it is frozen in `campaign.json` and `stage_run` reads
it from there, so a different model is a different experiment and needs its own
registration.

## Trials so far

| # | result | trades | note |
|---|---|---|---|
| 0 | +0.0185 | 153 | |
| 1 | **rejected** | 175 | exposed in 34 of 100 |
| 2 | **rejected** | 30 | exposed in 24 of 100 |
| 3 | +0.0125 | 153 | |
| 4 | **rejected** | 59 | exposed in 44 of 100 |
| 5 | — | — | (row 6 of 6) |

Three of the first five rejected by Amendment 6, all far below the floor.

**Every row records `sessions_exposed: null`** — the Amendment 6 disclosure fix
was incomplete and `ScoreTuple.as_dict()` never serialises it. Recorded in
`pre-registration.md`; not fixable while this campaign is mid-flight, and the
count is recomputable from each row's stored fill log.
