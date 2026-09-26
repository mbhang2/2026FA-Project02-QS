# nq-ml-04 is superseded, not resumable — 2026-09-26

**6 of 20 trials ran, all valid, none contaminated.** The campaign is left
exactly as it stopped and nothing has been deleted.

## Why it stopped

A model substitution at trial 6: the gateway answered a `deepseek-v4-pro`
request with `gemini-3.1-pro-preview` and the driver raised `ModelSubstituted`
before parsing the reply, so no ledger row and no strategy file were written.
`NOTES.md` has the detail. Minutes later `deepseek-v4-pro` began returning
`HTTP 404 Cannot find any route matching` while still appearing in the
gateway's `/models`.

## Why it is not being resumed

Two reasons, and the second is the binding one.

1. **`deepseek-v4-pro` is still unroutable**, and the model is frozen in
   `campaign.json` — `stage_run` reads it from the record, so waiting was the
   only way to resume it. Michael chose to move rather than wait.
2. **`src/core/loop/llm.py` has been edited since it stopped.** The preflight
   asked for `max_tokens: 16`, which a reasoning model spends before emitting a
   visible token, so the gateway reported the empty output as a content-policy
   violation and **no reasoning model could be registered at all**. Fixing that
   was necessary to register anything today. `src/core/**` is inside
   `code_fingerprint`, so trials 6-19 would have run under a different
   fingerprint from trials 0-5. The ledger would show the seam, which is worth
   something, but a campaign split across two versions of the driver is not one
   experiment.

## What replaces it

`nq-ml-05`: same corrected seed (`8fc28c884db7d7c7`), same profile, same
budget of 20, same 0.70 floor, proposer **`gemini-3.1-pro-preview`** — the one
model on that gateway currently verified to serve itself.

**The proposer changed for infrastructure reasons, not because results looked
bad.** Recorded because the distinction is the whole point: nq-ml-04's six
trials were +0.0185, rejected, rejected, +0.0125, rejected and one more, which
is neither encouraging nor discouraging, and nobody chose to abandon them on
the strength of it. A different proposer is a different experiment
(`docs/design/loop.md`), so nq-ml-05 starts at trial 0 and its result may not be
pooled with these six.
