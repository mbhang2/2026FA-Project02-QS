"""The three intelligence passes: decompose, propose, critique.

Each is one (or, under refund, a bounded few) gateway calls that return JSON,
parsed into typed objects and validated *mechanically* -- the model's discipline
is not trusted to be the guardrail, the schema/linters are. Every pass is
self-contained: it prompts only with what it is given, never with the gate and
never with market history as fact.

The firewall is enforced in three layers, and only the first is "trust the
model":

1. the *prompt* asks for a method, not a price history (advisory);
2. the *schema validator* (``researcher.schemas``) rejects non-conformant output,
   and ``researcher.leak_lint`` rejects realized-outcome prose (deterministic);
3. the *rail*'s ``src.core.loop.hypothesis.lint`` is the final authority before
   the file is ever written (the MVP's own gate, untouched).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import llm
from .llm import LLMError, ModelSubstituted
from .schemas import DataRequires, HypothesisRecord, InvalidRecord, SourceClaim
from .retrieval import SourceClip

PROPOSE_MODEL = "claude-haiku-4-5"
CRITIQUE_MODEL = "claude-haiku-4-5"
PROPOSE_TEMPERATURE = 0.7
CRITIQUE_TEMPERATURE = 0.0
MAX_REFINE_ATTEMPTS = 2

SYSTEM_RULES = (
    "You are a quantitative finance researcher. Write about mechanism and "
    "method, never about what a price actually did. Do not state a realized price "
    "move, a current or latest price level, a specific date, or a price magnitude "
    "for any instrument (NQ, ES, QQQ, S&P, Nasdaq). Do not use the words or "
    "phrases: significant, statistically, p-value, hold out, out-of-sample, "
    "in-sample, validation, false discovery, multiple testing, family-wise, "
    "correction, accept, reject, threshold, overfit, or any count of trials. "
    "Return JSON only."
)

# The data surface the campaign harness actually serves. `data_requires` must be
# honest about this: a mechanism that genuinely needs ticks is unrunnable here
# and will be rejected pre-flight, but the interval is not a creative choice.
DATA_SURFACE = (
    "The harness serves 5-minute bars only (no tick data, no spreads). Set "
    "data_requires.interval to \"5m\" and needs_ticks/needs_spreads to false; "
    "say so truthfully if a mechanism genuinely needs ticks or another interval."
)


@dataclass(frozen=True, slots=True)
class CriticVerdict:
    verdict: str  # "pass" | "fail"
    failures: tuple[str, ...]
    notes: str = ""


class PassBlocked(RuntimeError):
    """A generation pass cannot produce a usable result, and is recorded as such."""


def _ask(model: str, temperature: float, max_tokens: int, user: str) -> dict:
    """One call, parsed as JSON, or blocked."""
    try:
        comp = llm.call(model, [
            {"role": "system", "content": SYSTEM_RULES},
            {"role": "user", "content": user},
        ], temperature=temperature, max_tokens=max_tokens)
    except (LLMError, ModelSubstituted) as exc:
        raise PassBlocked(f"call failed: {exc}") from None

    data = llm.extract_json(comp.text)
    if data is None:
        raise PassBlocked(f"no JSON in reply ({len(comp.text)} chars)")
    return data


def _sources_text(sources: list[SourceClip]) -> str:
    lines = []
    for i, s in enumerate(sources):
        lines.append(f"[{i}] {s.title} ({s.url})\n    {s.summary[:600]}")
    return "\n\n".join(lines) if lines else "(no sources retrieved)"


# -- decompose --------------------------------------------------------------- #

def decompose(ask: str) -> list[str]:
    """One ask -> several *mechanism families*. Coverage, not a single "winner"."""
    user = (
        f"Research ask: {ask}\n\n"
        "If the ask is phrased as \"predict the next price\" or similar, note "
        "that no strategy predicts with certainty -- there is no such thing. "
        "Interpret the ask as: is there a *detectable, tradable statistical "
        "edge* (a conditional tendency or anomaly in prices/returns), and "
        "decompose THAT.\n\n"
        "Decompose into 3 to 6 distinct, concrete mechanism families -- momentum, "
        "mean-reversion, microstructure/order-flow, calendar effects, "
        "cross-asset, volatility regime, etc. Make each phrase specific enough "
        "to serve as a search query (e.g. \"intraday momentum in equity index "
        "futures\", not \"prices move around\"). Return JSON:\n"
        '{"families": ["one-line family", ...]}'
    )
    data = _ask(PROPOSE_MODEL, PROPOSE_TEMPERATURE, 1024, user)
    families = data.get("families")
    if not isinstance(families, list) or not families:
        raise PassBlocked("no families in reply")
    return [str(f).strip() for f in families if str(f).strip()]


# -- propose ---------------------------------------------------------------- #

def propose(mechanism: str, sources: list[SourceClip]) -> HypothesisRecord:
    """One mechanism -> one `HypothesisRecord`, validated by the schema."""
    user = (
        f"Mechanism to research: {mechanism}\n\n"
        f"{DATA_SURFACE}\n\n"
        "Sources retrieved for it (cite only these; do not invent a citation):\n"
        f"{_sources_text(sources)}\n\n"
        "Form ONE concrete, falsifiable hypothesis. Return JSON exactly:\n"
        '{"id":"slug-like-id","mechanism":"one-sentence economic story",'
        '"falsifiable":"ONE if-true-then-X sentence",'
        '"operationalize":"features that realize it",'
        '"data_requires":{"interval":"5m","needs_ticks":false,'
        '"needs_spreads":false,"other":""},'
        '"sources":[{"claim":"one supported sentence","source_url":"https://...",'
        '"source_title":"..."}],"confidence_prior":"why it is worth a trial"}'
    )
    data = _ask(PROPOSE_MODEL, PROPOSE_TEMPERATURE, 2048, user)
    try:
        return _record_from_json(data)
    except (InvalidRecord, KeyError, TypeError) as exc:
        raise PassBlocked(f"propose did not conform: {exc}") from None


def _record_from_json(data: dict) -> HypothesisRecord:
    dr = data.get("data_requires") or {}
    sources = tuple(
        SourceClaim(claim=str(sc.get("claim", "")),
                    source_url=str(sc.get("source_url", "")),
                    source_title=str(sc.get("source_title", "")))
        for sc in (data.get("sources") or [])
    )
    return HypothesisRecord(
        id=str(data["id"]),
        mechanism=str(data["mechanism"]),
        falsifiable=str(data["falsifiable"]),
        operationalize=str(data["operationalize"]),
        data_requires=DataRequires(
            interval=str(dr.get("interval", "5m")),
            needs_ticks=bool(dr.get("needs_ticks", False)),
            needs_spreads=bool(dr.get("needs_spreads", False)),
            other=str(dr.get("other", "")),
        ),
        sources=sources,
        confidence_prior=str(data.get("confidence_prior", "")),
    )


# -- critique --------------------------------------------------------------- #

def critique(rec: HypothesisRecord) -> CriticVerdict:
    """A colder second pass against a literal checklist."""
    record_json = json.dumps({
        "id": rec.id, "mechanism": rec.mechanism, "falsifiable": rec.falsifiable,
        "operationalize": rec.operationalize,
        "data_requires": {"interval": rec.data_requires.interval,
                          "needs_ticks": rec.data_requires.needs_ticks,
                          "needs_spreads": rec.data_requires.needs_spreads},
        "sources": [{"claim": s.claim, "source_url": s.source_url,
                     "source_title": s.source_title} for s in rec.sources],
        "confidence_prior": rec.confidence_prior,
    })
    user = (
        "Review this hypothesis for SEMANTIC quality only. Gate-vocabulary and "
        "realized-outcome checks are done separately by deterministic linters, so "
        "do not flag those. Check each rule and return JSON:\n"
        '{"verdict":"pass","failures":[],"notes":""} or '
        '{"verdict":"fail","failures":["<name>"],"notes":""}\n'
        "Failure names and their PRECISE meaning — do not broaden them:\n"
        "- not_falsifiable: `falsifiable` is not ONE if-then sentence that names "
        "a market OBSERVABLE that would differ. (Predicting a direction or sign "
        "of an effect is CORRECT, not a failure.)\n"
        "- not_runnable: `operationalize` is not computable from bar/price data "
        "alone (e.g. needs order-book depth or volume unavailable as bars).\n"
        "Note: whether a cited source was actually retrieved is checked "
        "separately and mechanically -- do not flag source provenance. Only "
        "flag on the two rules above.\n\n"
        "Only flag when a rule is clearly violated. Predicting \"positive "
        "returns if true\" is a falsifiable prediction, not a failure.\n\n"
        f"Hypothesis:\n```json\n{record_json}\n```"
    )
    data = _ask(CRITIQUE_MODEL, CRITIQUE_TEMPERATURE, 1024, user)
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in ("pass", "fail"):
        raise PassBlocked(f"critique returned unknown verdict {verdict!r}")
    failures = tuple(str(f) for f in (data.get("failures") or []))
    return CriticVerdict(verdict=verdict, failures=failures,
                         notes=str(data.get("notes", "")))


# -- the bounded refine loop ------------------------------------------------- #

def generate_hypothesis(mechanism: str, sources: list[SourceClip], *,
                        max_refine: int = MAX_REFINE_ATTEMPTS,
                        finalize=None) -> HypothesisRecord:
    """propose -> critique -> refine, capped; exhaust = blocked, recorded.

    `finalize`, if given, is a callable ``rec -> list[str]`` returning any
    *deterministic* lint failures (empty = clean). Its messages join the
    critic's failures so a gate-vocabulary or realized-outcome leak is *refined
    out*, not merely blocked at the end. The cap mirrors ``llm.py``'s bounded
    repair loop: a proposal that keeps failing is a recorded block, never a
    silent retry and never an infinite loop.
    """
    rec = propose(mechanism, sources)
    for attempt in range(max_refine + 1):
        verdict = critique(rec)
        problems: list[str] = []
        if verdict.verdict != "pass":
            problems.append("critic: " + ", ".join(verdict.failures))
        if finalize is not None:
            problems.extend(finalize(rec))
        if not problems:
            return rec
        if attempt == max_refine:
            raise PassBlocked(
                f"failed after {max_refine} refine attempts: "
                + " | ".join(problems))

        rec = _refine(rec, problems)

    raise PassBlocked("unreachable")  # pragma: no cover


def _refine(rec: HypothesisRecord, problems: list[str]) -> HypothesisRecord:
    """One directed rewrite of the *prose* fields only.

    `id`, `data_requires`, and `sources` are provenance + scope and stay frozen:
    sources came from retrieval (never re-invented), and the id is a stable slug.
    The critic and the deterministic lints review content, so only the text
    fields are rewritten.
    """
    user = (
        "This hypothesis failed review:\n- " + "\n- ".join(problems) +
        "\nFix exactly those problems and return JSON with only these fields:\n"
        '{"mechanism":"...","falsifiable":"...","operationalize":"...",'
        '"confidence_prior":"..."}\n'
        "Hard rule: `falsifiable` is exactly ONE sentence -- one period total, "
        "no internal sentence breaks. Write about mechanism and method only; do "
        "not mention how a result is tested or split (out-of-sample, in-sample, "
        "validation, hold out, overfit); do not state any realized price move, "
        "level, or date."
    )
    data = _ask(PROPOSE_MODEL, PROPOSE_TEMPERATURE, 2048, user)
    try:
        return HypothesisRecord(
            id=rec.id,
            mechanism=str(data.get("mechanism", rec.mechanism)),
            falsifiable=str(data.get("falsifiable", rec.falsifiable)),
            operationalize=str(data.get("operationalize", rec.operationalize)),
            data_requires=rec.data_requires,
            sources=rec.sources,
            confidence_prior=str(data.get("confidence_prior", rec.confidence_prior)),
        )
    except InvalidRecord as exc:
        raise PassBlocked(f"refine did not conform: {exc}") from None
