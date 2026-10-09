"""Optional NLI grounding check (MiniCheck), layered on the deterministic lints.

The deterministic lints -- ``leak_lint`` (realized-outcome prose), the rail's
gate-vocabulary lint, and the hallucinated-citation check -- are the *authority*:
reproducible, always run, and the only thing that can hard-block a hypothesis.

This module is a *second opinion*: if a MiniCheck NLI endpoint is configured and
reachable, each ``SourceClaim`` is checked for entailment against its source
text, and any ungrounded claim is returned as *advisory* refine feedback. It is
deliberately non-gating and non-deterministic:

- Off unless ``MINICHECK_API_BASE`` is set.
- Degrades to a no-op (empty findings) when the endpoint is absent, unreachable,
  or returns an ambiguous verdict -- it can never be the thing that blocks a
  hypothesis, and it can never invent a false failure.
"""

from __future__ import annotations

import os
import re

import requests

DEFAULT_MODEL = os.environ.get("MINICHECK_MODEL", "openai/minicheck-flan-t5-large")
_TIMEOUT = 10.0

_YES = {"yes", "supported", "entailed", "true", "1", "correct"}
_NO = {"no", "not supported", "not entailed", "false", "0", "incorrect",
       "unsupported"}


class GroundingUnavailable(RuntimeError):
    """The NLI endpoint is not configured or not reachable."""


def _norm_url(url: str) -> str:
    return re.sub(r"https?://", "", url).rstrip("/").lower()


def _ask(document: str, claim: str) -> bool | None:
    """One NLI call; ``None`` on any ambiguity (never a false block)."""
    base = os.environ.get("MINICHECK_API_BASE", "").rstrip("/")
    if not base:
        raise GroundingUnavailable("MINICHECK_API_BASE not set")
    prompt = (
        f"Document: {document}\n"
        f"Claim: {claim}\n"
        "Is the Claim supported by the Document? Answer only yes or no."
    )
    payload = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 8,
    }
    resp = requests.post(f"{base}/chat/completions", json=payload, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GroundingUnavailable(f"HTTP {resp.status_code}")
    try:
        text = resp.json()["choices"][0]["message"]["content"].strip().lower()
    except (ValueError, KeyError, IndexError):
        return None
    if text in _YES:
        return True
    if text in _NO:
        return False
    return None


def check_claims(rec, clips) -> list[str]:
    """Advisory NLI grounding findings, as refine-able feedback strings.

    Returns ``[]`` when the endpoint is absent/unreachable or a claim is
    ambiguous. A hallucinated citation (URL not among the clips) is *not* this
    module's job -- that is the deterministic lint's -- so it is skipped here.
    """
    if not os.environ.get("MINICHECK_API_BASE"):
        return []
    by_url = {_norm_url(c.url): c for c in clips}
    findings: list[str] = []
    for sc in rec.sources:
        clip = by_url.get(_norm_url(sc.source_url))
        if clip is None:
            continue
        try:
            supported = _ask(clip.summary[:2000], sc.claim)
        except (GroundingUnavailable, requests.RequestException):
            return []  # degrade to a no-op on any endpoint trouble
        if supported is False:
            findings.append(
                f"ungrounded: claim {sc.claim!r} is not supported by "
                f"{sc.source_title or sc.source_url}")
    return findings
