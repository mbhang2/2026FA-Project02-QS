# Running the loop against your own Anthropic key

The harness needs exactly two things: `LITELLM_BASE_URL` and `LITELLM_API_KEY`,
and an **OpenAI-compatible** `POST {base}/chat/completions` whose reply names the
model it served. Nothing else is assumed, so any endpoint meeting that contract
works and no code changes are required.

This document is how to point it at a locally-run LiteLLM proxy over an
Anthropic API key, written 2026-10-17 while the shared gateway was blocked on a
team budget.

## An API key, not a Claude Code subscription

A Pro or Max subscription powers the Claude Code client. It is not a
programmatic endpoint a harness can POST to, and using it as one is outside what
it is for. What this needs is an **API key** from the Anthropic console, which
is a different product.

## Install the proxy OUTSIDE `.venv` — this one matters

`integrity.environment_fingerprint()` hashes **every importable distribution**
in the run-time environment, and that fingerprint is written onto every scored
ledger row. Installing a proxy into `.venv` would move the fingerprint of every
subsequent trial for a library the engine never calls, and every existing row
would then read as environment-drifted.

The repository already has the pattern, for the PDF reader: a second
interpreter at `.venv-tools`, which nothing in the run-time path imports.

    python3 -m venv .venv-tools                 # exists already
    .venv-tools/bin/pip install 'litellm[proxy]'

## Configure it

`secrets/litellm-local.yaml` — `secrets/` is git-ignored, and the key belongs in
the environment rather than the file:

```yaml
model_list:
  - model_name: claude-haiku-4-5
    litellm_params:
      model: anthropic/claude-haiku-4-5
      api_key: os.environ/ANTHROPIC_API_KEY
  - model_name: claude-sonnet-5
    litellm_params:
      model: anthropic/claude-sonnet-5
      api_key: os.environ/ANTHROPIC_API_KEY
```

**`model_name` is what the harness asks for and what the reply must say.** Keep
the two sides identical. `preflight()` compares them and raises
`ModelSubstituted` when they differ -- which is the check that caught the shared
gateway serving `gemini-3.1-pro-preview` for ten different names on 2026-10-17,
and the one Task 37 was discarded for lacking.

## Run it, and point the harness at it

    export ANTHROPIC_API_KEY=...                       # never committed
    .venv-tools/bin/litellm --config secrets/litellm-local.yaml --port 4000

    export LITELLM_BASE_URL=http://127.0.0.1:4000/v1
    export LITELLM_API_KEY=anything-nonempty           # the proxy is local

Check before spending anything:

    python - <<'PY'
    import os, requests
    b, k = os.environ["LITELLM_BASE_URL"], os.environ["LITELLM_API_KEY"]
    r = requests.post(f"{b}/chat/completions",
                      headers={"Authorization": f"Bearer {k}"}, timeout=60,
                      json={"model": "claude-haiku-4-5", "max_tokens": 5,
                            "messages": [{"role": "user", "content": "reply with: ok"}]})
    print(r.status_code, r.json().get("model"))
    PY

It must print the model you asked for. If it prints anything else, stop: the
record cannot name a model that did not answer.

## The endpoint is recorded, so the switch documents itself

`agent_config.endpoint` is frozen into every `campaign.json`, so a campaign run
this way says `127.0.0.1` where session 07 says the shared gateway. That is the
record doing its job -- a reader can see which runs came from where.

## What this costs, in tokens the project actually used

Measured over session 07's three campaigns: **100k input and 48k output per
trial at N=400** (output counts completion plus reasoning, both billed as
output).

| run | input | output |
|---|---|---|
| Task 37, both arms at N=100 | ~12 M | ~6 M |
| one campaign at N=400 | ~40 M | ~19 M |

Task 37's arms are cheaper per trial than N=400 because the history block grows
with the campaign: its first eighty trials ran about 65% of the N=400 median.

Multiply by the console's current per-token rates. Roughly 20 M of the 40 M
input on an N=400 run is the same three reference documents re-sent on every
call, so prompt caching would cut it substantially -- the harness does not send
cache-control today, and adding it is a small change to `src/core/loop/llm.py`.

## Which model is a free choice, and which is not

**Task 37 must be `claude-haiku-4-5`.** Its whole design is that only the data
differs from Task 36, which ran that model; its pre-registration names it and
stands untouched. Any other model voids the comparison, which is exactly why the
first attempt was thrown away.

**A new campaign may name any model**, provided the record names the one that
answers.
