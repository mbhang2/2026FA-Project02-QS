"""Rejection: what this corpus already ruled out, and the duty to look.

`registry.py` records an `outcome` for a paper -- a campaign ran on it and the
campaign decided. Some of those outcomes are rejections: a trial that did not
clear the gate, a hypothesis the campaign could not support, a mechanism the
data refused. Those are the most expensive thing the corpus knows and the easiest
thing to forget, because a rejection produces no strategy and no champion.

The failure this module exists to prevent is a proposal arriving with no idea
what was already tried. A distil that has never seen a rejected trial will
happily re-propose it, and the tell is invisible: the campaign runs, the number
lands near zero, and the record shows no prior work. Nothing in the harness
stops that, because the harness measures outcomes and cannot know that the
outcome is a repeat.

So a distil is not allowed to be silent about prior rejections. It must name the
outcomes it looked at and turned down, and the names must resolve to real
registry rows. The pattern comes from the retrieval gate in
`tests/rag_research/docs/strategy_memo_negative.md`, where a fabricated
invalidation reference (`kb2_trial_104_sma_spread`) is the trap: a candidate that
cites a trial that never existed has engaged with nothing.

## Why a citation and not a ban

A ban -- "do not propose X" -- does not survive contact with a second paper. The
rejected set grows with every campaign, the rules multiply, and eventually the
linter is the pipeline: an agent optimizing a list of prohibitions is an agent
optimizing the linter, which is the same mistake
`hypothesis.py` was written to prevent. Naming the prior rejection keeps the
knowledge in the corpus where a later campaign can find it, and keeps the check
in the loop where it can be audited.

The check here is deliberately weak. It does not judge whether the proposal is
*good*, whether it *should* have been rejected, or whether the cited trial is
*relevant*. It asserts two things only: the rejection is named, and the name is
real. Both are checkable without a model and without a threshold, which is what
makes them worth enforcing mechanically. Relevance is the campaign's problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .registry import Registry, RegistryError

# An outcome that counts as a rejection. `promoted` is a rejection of nothing --
# it is the corpus saying yes -- so it is excluded by name rather than by
# inference, so that a new outcome kind fails loudly here instead of silently
# counting as consent.
REJECTING = ("rejected", "refuted", "inconclusive", "negative")

# A registry key: the base arXiv id, version stripped, e.g. `2601.10001`.
_KEY_RE = re.compile(r"\d{4}\.\d{4,5}")

# `prior_invalidation_rejected: kb2_trial_104_sma_spread` is the shape the RAG
# memos use, and it is the wrong shape for this corpus: a registry key is an
# arXiv id, so a citation that names a non-key cannot be resolved and is
# therefore a citation of nothing. Both are accepted so the memos' vocabulary
# reads as an error rather than as a miss.
_REJECTED_FIELD_RE = re.compile(
    r"^\s*(?:[-*]\s*)?prior_invalidation_rejected\s*:\s*(?P<value>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class RejectionUndisclosed(RegistryError):
    """The proposal did not say what it looked at and turned down."""


class RejectionUngrounded(RegistryError):
    """The proposal named a trial that is not in the registry."""


@dataclass(frozen=True, slots=True)
class Rejection:
    """One rejected trial, as a distil cites it."""

    key: str
    reason: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class RejectionDuty:
    """What the corpus had already ruled out when a distil ran.

    `rejected` is every rejection in the registry at the moment the distil was
    asked to propose. It is a value, not a query, so the duty a proposal has to
    discharge is fixed before the proposal is read and cannot be narrowed by the
    proposal itself.
    """

    rejected: tuple[Rejection, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return bool(self.rejected)

    def prompt_block(self) -> str:
        """The prior rejections, for the distil's prompt. Empty means none.

        Wording is the same shape `hypothesis.py` uses: state the market
        question, never the gate. A rejected trial's *reason* is corpus
        knowledge and belongs in the prompt; the gate's bar does not.
        """
        if not self.rejected:
            return ""
        lines = [
            "The corpus has already ruled out the following. Propose something "
            "that does not depend on them, and say which of them you looked at "
            "and turned down.",
            "",
        ]
        for r in self.rejected:
            suffix = f" -- {r.reason}" if r.reason else ""
            lines.append(f"- {r.key}{suffix}")
        return "\n".join(lines)


def duty(reg: Registry) -> RejectionDuty:
    """Every rejecting outcome the registry knows, in key order.

    A `fetch`/`screen` verdict is not an outcome and never appears here. An
    outcome naming neither a known rejecting kind nor `promoted` is an error:
    it means the campaign wrote a verdict this module cannot read, and treating
    it as consent is the failure this whole module is about.
    """
    out: list[Rejection] = []
    for rec in reg.events():
        if rec.get("kind") != "outcome":
            continue
        verdict = str(rec.get("verdict") or rec.get("outcome") or "").strip()
        key = str(rec.get("arxiv_id") or "")
        if verdict == "promoted":
            continue
        if verdict not in REJECTING:
            raise RegistryError(
                f"{key} has outcome verdict {verdict!r}, which is neither "
                "'promoted' nor one of "
                f"{', '.join(repr(v) for v in REJECTING)}. Add the new verdict "
                "to REJECTING or decide here what it means; a verdict this "
                "module cannot read must not count as consent."
            )
        out.append(Rejection(key=key, reason=str(rec.get("reason") or "")))
    return RejectionDuty(rejected=tuple(sorted(out, key=lambda r: r.key)))


def cited(text: str) -> tuple[str, ...]:
    """Every prior rejection the proposal names, in the order named.

    Reads the `prior_invalidation_rejected` field the RAG memos use. A proposal
    that cites several does so as a comma-separated list.
    """
    found: list[str] = []
    for m in _REJECTED_FIELD_RE.finditer(text or ""):
        for part in m.group("value").split(","):
            token = part.strip().strip("\"'`[]()")
            if token:
                found.append(token)
    return tuple(found)


def check(text: str, d: RejectionDuty) -> tuple[Rejection, ...]:
    """Assert the proposal discharged its duty. Return what it cited.

    Raises `RejectionUndisclosed` when the corpus had rejections and the
    proposal names none, and `RejectionUngrounded` when a name does not resolve
    to a registry row. When the corpus has no rejections there is nothing to
    disclose and the proposal passes -- the duty is a function of the corpus,
    not of the proposal, so an empty corpus cannot make a proposal wrong.
    """
    if not d.rejected:
        return ()

    names = cited(text)
    if not names:
        raise RejectionUndisclosed(
            "The corpus has already rejected "
            + ", ".join(r.key for r in d.rejected)
            + ", and this proposal names none of them. A proposal that does not "
            "say what it looked at and turned down will re-propose a trial that "
            "already ran. Cite them as "
            "`prior_invalidation_rejected: <arxiv-id>`."
        )

    known = {r.key for r in d.rejected}
    bad = [n for n in names if n not in known]
    if bad:
        raise RejectionUngrounded(
            "prior_invalidation_rejected names "
            + ", ".join(bad)
            + ", which is not a rejected trial in the registry. A citation that "
            "resolves to nothing is a citation of nothing. The rejected set is: "
            + (", ".join(sorted(known)) or "(empty)")
            + ". If the trial ran outside the registry, record its outcome "
            "first with `Registry.append(kind='outcome', ...)`."
        )

    order = {r.key: r for r in d.rejected}
    return tuple(order[n] for n in names)
