"""A minimal model client for the committee. Deliberately not the harness's.

**`audit/` imports nothing from `src.core` and nothing imports `audit/`** --
`tests/test_tier_contract.py` asserts it for `tier3.py`, and the whole point of
the tier boundary is that either side can be replaced without touching the other.
So the committee cannot reach `src.core.loop.llm`, and this is the duplication
that buys the separation. It is about sixty lines; the coupling it avoids is the
reason `audit/` can be swapped for somebody else's auditor.

The one thing copied rather than reinvented is the substitution check, because
Task 37 cost 35 trials to learn it: a gateway can answer a request for one model
with another, this one carries `fallbacks=[{'*': ['gemini-3.1-pro-preview']}]`,
and a reply that does not name a model cannot be checked and is therefore treated
as a substitution rather than as a pass.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


class LLMUnavailable(RuntimeError):
    """The endpoint did not produce a usable answer. Never retried."""


class ModelSubstituted(RuntimeError):
    """The endpoint answered with a different model, or would not say which.

    Deliberately not a subclass of `LLMUnavailable`: an unavailable model means
    the committee did not run, which is a CANNOT_TELL. A substituted model means
    the record would be false, which is not something to degrade gracefully past.
    """


@dataclass
class Client:
    model: str
    temperature: float = 0.0
    # 16384, not 4096. nq-ml-02's committee ran on deepseek-v4-pro, and three of
    # four seats came back empty: the model spent all 4,096 completion tokens on
    # reasoning_content and finish_reason was "length" before the JSON verdict
    # was written. Measured: the same economics prompt needs ~7,000 completion
    # tokens on that model, 6,900 of them reasoning. Robustness, the shortest
    # prompt, was the one seat that fit.
    max_tokens: int = 16384
    # A verdict is a short JSON object; long chains of thought are cost, not
    # rigour. Sent when set; dropped and retried once if the endpoint rejects
    # the parameter for a model that does not take it.
    reasoning_effort: str | None = "low"
    timeout_s: float = 180.0
    base_url: str = ""
    calls: list = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url or os.environ.get("LITELLM_BASE_URL", "")
        if not self.base_url or not os.environ.get("LITELLM_API_KEY"):
            raise LLMUnavailable(
                "LITELLM_BASE_URL and LITELLM_API_KEY must both be set; the "
                "committee needs a model and will not silently skip itself")

    def ask(self, system: str, user: str) -> dict:
        """One call, one JSON object back. No retries, by design."""
        import requests

        body = {"model": self.model, "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"}
        resp = requests.post(f"{self.base_url}/chat/completions", json=body,
                             headers=headers, timeout=self.timeout_s)
        if (resp.status_code == 400 and "reasoning_effort" in body
                and "reasoning_effort" in resp.text):
            # The one retry this client makes, and only for a parameter the
            # model does not accept -- not for a verdict it did not like.
            body.pop("reasoning_effort")
            resp = requests.post(f"{self.base_url}/chat/completions", json=body,
                                 headers=headers, timeout=self.timeout_s)
        if resp.status_code != 200:
            raise LLMUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        served = str(payload.get("model") or "")
        if not served:
            raise ModelSubstituted(
                f"the endpoint answered for {self.model!r} without naming a "
                "model, so substitution cannot be ruled out")
        if not _same(self.model, served):
            raise ModelSubstituted(f"asked {self.model!r}, served {served!r}")

        choice = payload["choices"][0]
        text = choice["message"].get("content") or ""
        usage = payload.get("usage") or {}
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        self.calls.append({
            "model_served": served,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": reasoning_tokens,
            "finish_reason": choice.get("finish_reason"),
        })
        if not text.strip():
            # Say what happened, not "not JSON: ''". An auditor cut off by
            # max_tokens and one that returned nothing are different failures
            # and the artefact has to tell them apart.
            raise LLMUnavailable(
                f"empty reply: finish_reason={choice.get('finish_reason')!r}, "
                f"completion_tokens={usage.get('completion_tokens')}, "
                f"reasoning_tokens={reasoning_tokens}, max_tokens={self.max_tokens}")
        block = FENCE.search(text)
        raw = block.group(1) if block else text
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"reply was not JSON: {exc}; {text[:200]!r}")


def _same(asked: str, served: str) -> bool:
    """Ids get decorated -- a provider prefix, a dated snapshot, a deployment
    suffix are the same model. `gemini` for `claude` is not."""
    a, b = asked.rsplit("/", 1)[-1].lower(), served.rsplit("/", 1)[-1].lower()
    return a.startswith(b) or b.startswith(a)


# **The probe's budget has to cover the model's reasoning, not just its answer.**
# At 16 this raised on every reasoning model: the budget is spent before a visible
# token is emitted, and the gateway reports the empty output as
# `ContentPolicyViolationError: Response output was blocked`, which reads like a
# refusal. Measured 2026-09-25 on gemini-3.1-pro-preview: 4 of 4 failures at 16,
# HTTP 200 with an honest `model` field at 64.
#
# **Fifth site of one defect.** This client at `max_tokens=4096` (three of
# nq-ml-02's four seats empty), `LLMAgent.preflight` at 16, the proposer at 16,384
# (4 of nq-ml-05's first 10 trials), this function, and it is called by
# `screen_papers.py`, `rank_papers.py` and `review.py`. Each time the visible
# symptom pointed somewhere other than the budget.
PREFLIGHT_MAX_TOKENS = 512


def preflight(model: str) -> str:
    """What the endpoint actually serves. Raises rather than guessing."""
    c = Client(model=model, max_tokens=PREFLIGHT_MAX_TOKENS)
    import requests

    resp = requests.post(
        f"{c.base_url}/chat/completions",
        json={"model": model, "max_tokens": PREFLIGHT_MAX_TOKENS, "temperature": 0,
              "messages": [{"role": "user", "content": "reply with: ok"}]},
        headers={"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"},
        timeout=60)
    if resp.status_code != 200:
        raise LLMUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")
    served = str(resp.json().get("model") or "")
    if not served:
        raise ModelSubstituted("the endpoint named no model")
    if not _same(model, served):
        raise ModelSubstituted(f"asked {model!r}, served {served!r}")
    return served
