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

# The only per-bar fields the harness serves. Single source of truth: the
# validator below and the proposer's prompt (generate.DATA_SURFACE) both read it.
ALLOWED_FIELDS = frozenset({"open", "high", "low", "close", "volume", "vwap"})
MAX_PARAMS: int = 4
MAX_GRID_SIZE: int = 10_000

# -- lookahead lint ---------------------------------------------------------- #
# A forward bar index: {t+1}, [t+e], (t+N), _t+1, bare t+1. `\bt` and the
# explicit bracket/underscore forms keep `n_fast+1` and `score_t + 1` (plain
# arithmetic) from matching.
_FORWARD_IDX = r"(?:[\[{(]\s*t\s*\+\s*\w|_t\+\w|\bt\+\w)"
# Forward-looking prose. `futures` (the instrument) deliberately does not match.
# Negative phrases like 'without lookahead' or 'no lookahead' do not match.
_FUTURE_PROSE = (
    r"\bfuture\s+(?:bars?|returns?|prices?|closes?|data|information|values?)\b"
    r"|\bnext\s+bar'?s?\s+(?:close|high|low|return|price)\b"
    r"|(?<!without\s)(?<!no\s)\blook(?:ing)?[- ]?ahead\b|\bforward[- ]looking\b"
)
_LOOKAHEAD_RE = re.compile(f"{_FORWARD_IDX}|{_FUTURE_PROSE}", re.IGNORECASE)
# The causal use of future bars: an order *filled* or *exited* at bar t+k. Requires an explicit
# `at`/`on`/`@`/`after`/`for` so a forward bar used as a condition input never qualifies.
_FILL_RE = re.compile(
    r"(?:"
    r"(?:\bat\b|\bon\b|@|\bafter\b|\bfor\b|\buntil\b|\bby\b|\bin\b)\s+(?:the\s+)?(?:"
    r"(?:open|close)\s+of\s+(?:the\s+)?(?:bar\s+)?(?:next\s+bar|t\s*\+\s*\w+)"
    r"|next\s+bar'?s?\s+(?:open|close)"
    r"|(?:open|close)?\s*[_\[{(]{1,2}\s*t\s*\+\s*\w+\s*[\]})]{0,2}"
    r"|(?:bar\s+)?(?:open|close)?\s*[_\[{(]{0,2}\s*t\s*\+\s*\w+\s*[\]})]{0,2}"
    r")"
    r"|(?:open|entry|fill)(?:_price)?\s*[_\[{(]{0,2}\s*t\s*\+\s*\w+\s*[\]})]{0,2}"
    r"|\bbar\s*[_\[{(]{0,2}\s*t\s*\+\s*\w+\s*[\]})]{0,2}"
    r")",
    re.IGNORECASE,
)


def find_lookahead(text: str, *, allow_fill: bool) -> str | None:
    """The first lookahead token in `text`, or None if it is causal.

    `signal_def` (allow_fill=False): any forward reference is rejected -- a signal
    is computed from data up to and including bar t, nothing later.
    `entry_rule` / `exit_rule` (allow_fill=True): an order may be *filled* at the
    next bar ("enter at the open of the next bar"), because a strategy cannot fill
    on the bar that produced its signal. Fill phrases are blanked first; any forward
    reference left over (a condition on a later bar) is still rejected.
    """
    if not allow_fill:
        fill = _FILL_RE.search(text)
        if fill:
            return fill.group(0)
    scrubbed = _FILL_RE.sub(" <FILL> ", text) if allow_fill else text
    m = _LOOKAHEAD_RE.search(scrubbed)
    return m.group(0) if m else None


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
class ParameterDef:
    name: str
    min_val: float
    max_val: float
    step: float

    @property
    def grid_size(self) -> int:
        if self.step <= 0:
            return 1
        return max(1, int((self.max_val - self.min_val) / self.step) + 1)


@dataclass(frozen=True, slots=True)
class SpecData:
    signal_def: str
    entry_rule: str
    exit_rule: str
    parameters: tuple[ParameterDef, ...]
    bar_size: str
    session_filter: str
    cost_assumption: str
    data_fields_required: tuple[str, ...]

    def get_total_grid_size(self) -> int:
        total = 1
        for p in self.parameters:
            total *= p.grid_size
        return total


@dataclass(frozen=True, slots=True)
class HypothesisRecord:
    """The contract. One falsifiable mechanism, source-bound, data-scoped."""

    id: str
    mechanism: str            # WHY it should work -- the economic/behavioral story
    falsifiable: str          # a single if-true-then-X sentence
    operationalize: str       # features, in the strategy API's vocabulary
    specification: SpecData   # the precise typed spec
    data_requires: DataRequires
    sources: tuple[SourceClaim, ...]
    confidence_prior: str = ""
    bwis_score: float = 0.523   # posterior-weighted source quality; global floor default

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
        
        # C1 Typed Spec checks
        if not self.specification.signal_def.strip():
            raise InvalidRecord("specification.signal_def is empty")
        if not self.specification.entry_rule.strip():
            raise InvalidRecord("specification.entry_rule is empty")
        if not self.specification.exit_rule.strip():
            raise InvalidRecord("specification.exit_rule is empty")
        if not self.specification.cost_assumption.strip():
            raise InvalidRecord("specification.cost_assumption is empty -- must include spread/slippage model")
            
        for f in self.specification.data_fields_required:
            if f.lower() not in ALLOWED_FIELDS:
                raise InvalidRecord(
                    f"specification requires unknown data field: {f} "
                    f"(allowed: {', '.join(sorted(ALLOWED_FIELDS))})")

        spec = self.specification
        for label, text, allow_fill in (
                ("signal_def", spec.signal_def, False),
                ("entry_rule", spec.entry_rule, True),
                ("exit_rule", spec.exit_rule, True)):
            hit = find_lookahead(text, allow_fill=allow_fill)
            if hit:
                raise InvalidRecord(
                    f"lookahead bias detected in specification rule: {label} "
                    f"uses {hit!r}")

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
