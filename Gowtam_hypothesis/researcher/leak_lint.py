"""The researcher's *additional* lint, layered on top of the rail's gate lint.

``src.core.loop.hypothesis.lint`` rejects gate vocabulary ("significant",
"p-value", "hold out", ...). That is the *proposer's* failure mode. The
researcher has a different one: it reads the web, and it can "know" the realized
price path of the instrument being evaluated. A hypothesis that smuggles that
path into prose -- "NQ declined 4% in March", "the current price is ..." -- turns
the pre-registered split decorative without tripping the gate lint.

This module catches *that*: date/instrument/outcome statements and current-price
references. It is a mechanism (a fixed list of patterns the driver runs over
rendered prose), not a prompt sentence the model could paraphrase around. It is
deliberately over-broad: a human rephrasing one line loses a minute; a hypothesis
that leaks the realized path costs the campaign and nobody notices.
"""

from __future__ import annotations

import re

# Instruments the harness evaluates. Kept explicit rather than "any uppercase":
# a claim about a ticker is a leak only when the ticker is one the campaign
# scores on.
_TICKERS = r"(?:NQ|ES|QQQ|MNQ|MES|SPX|Nasdaq(?:-100)?|S&P|NDX)"

_DIRECTION = (
    r"declin\w*|fell|fallen|rose|risen|rallied|dropped|gained|lost|"
    r"plunged|surged|climbed|slipped|sold\s*off|rall(?:y|ied)|"
    r"moved\s+(?:up|down|higher|lower)|recovered|lost\s+ground|"
    r"reached(?:ing)?|hit|touched|printed|closed\s+(?:at|up|down)"
)

_MAGNITUDE = r"[+-]?\d+(?:\.\d+)?\s*(?:%|percent|points?|pts?)"

_OUTCOME = rf"(?:{_DIRECTION})"          # e.g. "declined", "closed up"
_MOVE = rf"(?:{_DIRECTION})\s+{_MAGNITUDE}"   # e.g. "fell 4%"

# A realized outcome: a ticker near a directional statement, date, or magnitude.
PATTERNS: tuple[tuple[str, str], ...] = (
    (rf"\b{_TICKERS}\b[^.\n]{{0,60}}\b(?:{_MOVE}|{_OUTCOME})\b",
     "states a realized price move for an evaluated instrument"),
    (rf"\b(?:{_OUTCOME})\b[^.\n]{{0,60}}\b{_TICKERS}\b",
     "states a realized price move for an evaluated instrument"),
    # A ticker's *current* level is a leak even without a number ("NQ's current
    # price"), but bare "current price" is strategy vocabulary and is NOT a leak.
    (rf"\b{_TICKERS}\b[^.\n]{{0,40}}\b(?:current|latest|today'?s)\s+"
     r"(?:price|close|level|quote|value)\b",
     "a ticker's current price level"),
    (r"\b(?:current|latest|today'?s)\s+(?:price|close|level|quote|value)\b"
     r"[^.\n]{0,20}\b\d[\d,.]*\b",
     "a current price stated with a numeric level"),
    (r"\b(?:over|during|in)\s+(?:the\s+)?(?:last|past|previous)\s+"
     r"\d+\s+(?:sessions?|days?|weeks?|months?|years?)\b",
     "anchors a claim to a realized window of time"),
    (rf"\b{_TICKERS}\b[^.\n]{{0,80}}\b(?:{_MAGNITUDE})\b",
     "pairs an evaluated instrument with a price magnitude"),
    (r"\b\d{4}\s*[-–]\s*\d{2}\s*[-–]\s*\d{2}\b",
     "a specific calendar date is a realized outcome, not a method"),
)


class OutcomeLeak(ValueError):
    """A hypothesis states a realized price outcome."""


def findings(text: str) -> list[str]:
    out: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern, why in PATTERNS:
            for m in re.finditer(pattern, line, flags=re.IGNORECASE):
                out.append(f"line {lineno}: {m.group(0)!r} -- {why}")
    return out


def lint(text: str, *, source: str = "<hypothesis>") -> None:
    found = findings(text)
    if not found:
        return
    raise OutcomeLeak(
        f"{source} reports a realized outcome for an evaluated instrument:\n  "
        + "\n  ".join(found)
        + "\n\nA hypothesis describes a mechanism, not what the price did. "
        "Rephrase so no dated move, current level, or price magnitude for NQ/ES/"
        "QQQ appears. See researcher/leak_lint.py for why this is enforced."
    )
