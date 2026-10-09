"""The gateway client: one HTTP round-trip per pass, with honest accounting.

This mirrors ``src/core/loop/llm.py`` (the MVP orchestrator) rather than
re-using it, because the researcher's output is JSON hypotheses, not Python
strategy source, and the two must not share a module the strategy child is
screened against. The disciplines are the same, because they are the design:

- **Record ``model_served`` and raise on substitution.** A pass answered by a
  model other than the one asked for is a harness failure, not a research result.
- **No retries inside ``call``.** A failure, empty reply, or unparseable JSON is
  raised to the caller, which records it as a blocked step -- never silently
  re-dispatched. ``gateway`` was caught by Team 37 serving a different model
  behind a wildcard fallback; that is precisely the failure this class guards.
- **Structured parse, not persuasion.** Output is read from a fenced JSON block.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import requests

DEFAULT_BASE_URL = os.environ.get("LITELLM_BASE_URL", "https://llm-proxy.invalid/v1")


def _provider(base_url: str) -> str:
    """A human hint about the gateway, from the URL only (never the key)."""
    host = base_url.split("://")[-1].split("/")[0].lower()
    if "llm-proxy" in host:
        return "team LiteLLM gateway"
    return host


class LLMError(RuntimeError):
    """The call did not produce a usable answer. Never retried.

    The message never contains the API key. Extra attributes carry the HTTP
    context a caller needs to triage a failure (WAF 403 vs LiteLLM 403 vs 400
    vs auth), so a block is reported as "HTTP 403 ..." instead of a misleading
    "no JSON".
    """

    def __init__(self, msg: str, *, status: int | None = None,
                 model: str = "", endpoint: str = "", provider: str = "",
                 content_type: str = "", body: str = "") -> None:
        super().__init__(msg)
        self.status = status
        self.model = model
        self.endpoint = endpoint
        self.provider = provider
        self.content_type = content_type
        self.body = body  # already truncated by the caller; never the key


class ModelSubstituted(RuntimeError):
    """The endpoint answered with a different model than the one asked for."""


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model_served: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


def api_key() -> str:
    """The key, `LITELLM_API_KEY` with a `Litellm` fallback (historical env name)."""
    return os.environ.get("LITELLM_API_KEY") or os.environ.get("Litellm") or ""


def _same_model(requested: str, served: str) -> bool:
    a = requested.rsplit("/", 1)[-1].lower()
    b = served.rsplit("/", 1)[-1].lower()
    return a.startswith(b) or b.startswith(a)


def call(model: str, messages: list[dict], *, temperature: float = 0.7,
         max_tokens: int = 4096, base_url: str = DEFAULT_BASE_URL,
         timeout: float = 240.0) -> Completion:
    """One completion. One. Raises on failure; the caller records and moves on."""
    key = api_key()
    if not key:
        raise LLMError("LITELLM_API_KEY is not set; the driver has no gateway key")

    body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": messages}
    try:
        resp = requests.post(f"{base_url}/chat/completions", json=body,
                             headers={"Authorization": f"Bearer {key}"},
                             timeout=timeout)
    except requests.RequestException as exc:
        raise LLMError(f"{type(exc).__name__}: {exc}") from None

    if resp.status_code != 200:
        raise LLMError(
            f"HTTP {resp.status_code}",
            status=resp.status_code, model=model,
            endpoint=f"{base_url}/chat/completions",
            provider=_provider(base_url),
            content_type=resp.headers.get("content-type", ""),
            body=resp.text[:300],
        )

    try:
        payload = resp.json()
        usage = payload.get("usage") or {}
        text = payload["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError) as exc:
        raise LLMError(
            f"unparseable envelope: {exc}",
            status=resp.status_code, model=model,
            endpoint=f"{base_url}/chat/completions",
            provider=_provider(base_url),
            content_type=resp.headers.get("content-type", ""),
            body=resp.text[:300],
        ) from None

    served = str(payload.get("model") or "")
    if served and not _same_model(model, served):
        raise ModelSubstituted(f"asked for {model!r}, endpoint answered {served!r}")
    if not served:
        # An endpoint that will not say what it served cannot be checked, and
        # "cannot be checked" is not "fine" -- same call as MVP llm.py Task 37.
        raise ModelSubstituted(
            f"endpoint answered for {model!r} without naming a model")

    return Completion(text=text, model_served=served,
                      prompt_tokens=int(usage.get("prompt_tokens") or 0),
                      completion_tokens=int(usage.get("completion_tokens") or 0))


def preflight(model: str, *, base_url: str = DEFAULT_BASE_URL,
              timeout: float = 240.0) -> dict:
    """One cheap call to confirm the gateway serves `model`. Raises on mismatch."""
    comp = call(model, [{"role": "user", "content": "reply with: ok"}],
                temperature=0, max_tokens=8, base_url=base_url, timeout=timeout)
    return {"requested": model, "served": comp.model_served}


_JSON_FENCE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def extract_json_text(text: str) -> str | None:
    """The last fenced JSON block, or a bare object; `None` if nothing parses as JSON.

    Mirrors ``extract_source``'s "take the last fence" rule: a model that thinks
    out loud then answers puts the answer last.
    """
    if not text or not text.strip():
        return None
    blocks = [b.strip() for b in _JSON_FENCE.findall(text) if b.strip()]
    if blocks:
        candidate = blocks[-1]
        if _is_json(candidate):
            return candidate
        return None
    # No fence: accept the whole reply only if it is (or contains) one JSON value.
    if _is_json(text):
        return text.strip()
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        candidate = text[first:last + 1].strip()
        if _is_json(candidate):
            return candidate
    return None


def _is_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except (ValueError, TypeError):
        return False


def extract_json(text: str) -> dict | None:
    """`extract_json_text` then parse. `None` is a blocked step, not a retry."""
    raw = extract_json_text(text)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
