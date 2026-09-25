"""The researcher's frozen output contract.

A hypothesis is *not prose*; it is a structured record the researcher emits and a
deterministic renderer flattens into the markdown the MVP's rail already reads
(``src.core.loop.hypothesis``). Structuring it is what turns the three questions
that make a hypothesis usable -- is it falsifiable, is it expressible on our data,
does it need data we have -- into mechanical checks instead of a reviewer's
judgement.

Everything here is ``frozen=True``. A ``HypothesisRecord`` is a candidate
*pre-registration*: once the renderer flattens it and the file is committed, the
commit date is the pre-registration date, exactly as ``hypothesis.py`` documents
for hand-written files. Frozen, so nothing downstream can mutate a claim after it
was born.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidRecord(ValueError):
    """A record that fails a mechanical rule, named by which rule.

    The researcher's LLM pass fills these fields; a non-conformant answer is
    rejected here, not coerced. ``InvalidRecord`` is the harness saying "this is
    not a hypothesis", and the driver converts it into a blocked proposal that is
    recorded, mirroring ``src.core.loop.llm``'s "a failed generation is a recorded
    trial, never a silent retry".
    """


@dataclass(frozen=True, slots=True)
class SourceClaim:
    """One sentence fragment and the exactly-one source it came from.

    Every claim carries provenance or it does not exist. A claim with no URL is
    dropped at construction, not "context what the model happened to remember".
    """

    claim: str
    source_url: str
    source_title: str = ""


@dataclass(frozen=True, slots=True)
class DataRequires:
    """What the hypothesis needs, in the subscription surface's vocabulary.

    ``needs_ticks`` and ``needs_spreads`` exist so the feasibility pre-flight can
    reject a hypothesis *before* a campaign wastes a trial on data the engine
    cannot serve. ``interval`` is a bar interval string ("5m", "1m", "1d", ...).
    """

    interval: str = "5m"
    needs_ticks: bool = False
    needs_spreads: bool = False
    other: str = ""


@dataclass(frozen=True, slots=True)
class HypothesisRecord:
    """The contract. One falsifiable mechanism, source-bound, data-scoped."""

    id: str
    mechanism: str            # WHY it should work -- the economic/behavioral story
    falsifiable: str          # a single if-true-then-X sentence
    operationalize: str       # features, in the strategy API's vocabulary
    data_requires: DataRequires
    sources: tuple[SourceClaim, ...]
    confidence_prior: str = ""

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id):
            raise InvalidRecord(f"id {self.id!r} must match {_ID_RE.pattern}")
        if not self.mechanism.strip():
            raise InvalidRecord("mechanism is empty")
        if not self.falsifiable.strip():
            raise InvalidRecord("falsifiable is empty -- a claim that is not "
                                "falsifiable is not a hypothesis")
        # A falsifiable statement must be a single if-then sentence, else the
        # campaign has nothing to test and the gate has nothing to reject.
        if _is_multi_sentence(self.falsifiable):
            raise InvalidRecord(
                f"falsifiable is not a single claim: {self.falsifiable!r}")
        if not self.operationalize.strip():
            raise InvalidRecord("operationalize is empty -- a mechanism that "
                                "cannot be expressed as features is a short story")
        if not self.sources:
            raise InvalidRecord("no sources -- an unsourced claim is dropped, "
                                "not emitted")
        if any(not sc.claim.strip() or not sc.source_url.strip()
               for sc in self.sources):
            raise InvalidRecord("every SourceClaim needs both a claim and a URL")


_ABBREV_DOT = re.compile(
    r"\b(e\.g|i\.e|etc|vs|cf|al|et\s+al|approx|fig|eq|no|st|dr|mr|ms|inc|corp"
    r"|sec|ref|vol)\.",
    re.IGNORECASE,
)


def _is_multi_sentence(text: str) -> bool:
    """True if the claim contains more than one terminated sentence.

    Abbreviation periods (``e.g.``, ``i.e.``, ``vs.``, ``etc.``) are blanked
    first, so a claim like "if X holds then Y, e.g. Z" is one sentence, not
    three. A claim that splits into multiple real sentences is not a single
    falsifiable statement and is rejected.
    """
    cleaned = _ABBREV_DOT.sub(lambda m: m.group(0)[:-1], text.strip())
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    return len(parts) > 1
