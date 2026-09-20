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
    max_tokens: int = 4096
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
        resp = requests.post(
            f"{self.base_url}/chat/completions", json=body,
            headers={"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"},
            timeout=self.timeout_s)
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

        text = payload["choices"][0]["message"]["content"]
        self.calls.append({
            "model_served": served,
            "prompt_tokens": (payload.get("usage") or {}).get("prompt_tokens"),
            "completion_tokens": (payload.get("usage") or {}).get("completion_tokens"),
        })
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


def preflight(model: str) -> str:
    """What the endpoint actually serves. Raises rather than guessing."""
    c = Client(model=model, max_tokens=16)
    import requests

    resp = requests.post(
        f"{c.base_url}/chat/completions",
        json={"model": model, "max_tokens": 16, "temperature": 0,
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
