"""Does a hypothesis state what the market actually did? A string check.

**Gowtam's idea** (`Gowtam_hypothesis/researcher/leak_lint.py` on
`origin/Gowtam/HypInt`), ported rather than merged, like `bwis.py`.
`hypothesis.lint` bans the gate's vocabulary -- "validation", "p-value",
"significant" -- which is the *proposer's* failure. A writer has a different
one: a model has read the news, and can "know" the realised path of the
instrument it is writing about. "NQ fell 4% in March" in a hypothesis is pasted
into every prompt of the campaign and turns the train split into a hint, without
tripping the gate lint at all.

**Why it matters more now.** A question written from a researcher's statement
at registration runs without anybody reading it first (Amendment 15). A human
would strike "NQ rallied 9% after the March FOMC" on sight; with no human, a
pattern has to.

**Tuned against the 25 hypotheses on record, which every version of this has to
pass.** His patterns as written flagged 19 of them, every one a false positive:

* *Any calendar date* is a leak, so "Written 2026-09-21, before the campaign"
  -- the provenance line most of our files carry -- failed. A date is only a
  leak next to a price move, so that is what is checked.
* *Instrument plus a number* is a leak, so "NQ's minimum tick is 0.25 points"
  failed. Contract facts are not outcomes; an instrument next to a *move* is.
* *"Over the last 20 sessions"* is a leak, which is how half of all strategies
  describe a lookback window. Dropped: a window next to a move is caught by the
  move.

What it still finds on record is real: two Amendment 11 refinements quote an
earlier campaign's profit by month ("2025-12 lost 438.83"). A refinement may
read train results, so they stand there -- which is why this runs in
`scripts/write_hypothesis.py`, on questions written from a researcher's words,
and not in `hypothesis.lint`, which every registration runs. A question with no
campaign behind it has no business quoting a dated result.

Deliberately still broad where a miss is expensive: a human rewording one line
loses a minute, a leaked path costs a campaign and nobody notices.
"""
from __future__ import annotations

import re

# The instruments a campaign scores on. Explicit, not "any capitals": a claim
# about a ticker is a leak only when the ticker is one we evaluate.
TICKERS = r"(?:NQ|MNQ|ES|MES|QQQ|NDX|SPX|SPY|Nasdaq(?:-100)?|S&P(?:\s*500)?)"

# Past-tense or completed moves only. "hits", "reaches" and "touches" are how a
# strategy describes its own entry ("when price hits the range high") and are
# not here; "hit" and "reached" in the past tense next to a ticker are.
DIRECTION = (
    r"declined|fell|fallen|rose|risen|rallied|dropped|gained|lost|plunged|"
    r"surged|climbed|slipped|sold\s*off|crashed|soared|tumbled|recovered|"
    r"closed\s+(?:at|up|down|higher|lower)|ended\s+(?:the\s+\w+\s+)?(?:up|down|higher|lower)|"
    r"traded\s+(?:up|down|higher|lower)|"
    r"(?:was|were)\s+(?:up|down)"
)
MAGNITUDE = r"[+-]?\d[\d,]*(?:\.\d+)?\s*(?:%|percent|points?|pts?|handles?)"
DATE = (r"(?:\b\d{4}\s*[-/]\s*\d{1,2}(?:\s*[-/]\s*\d{1,2})?\b|"
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}\b|"
        r"\b(?:19|20)\d{2}\b)")

PATTERNS: tuple[tuple[str, str], ...] = (
    (rf"\b{TICKERS}\b[^.\n]{{0,60}}\b(?:{DIRECTION})\b",
     "a realised move of an instrument we score on"),
    (rf"\b(?:{DIRECTION})\b[^.\n]{{0,40}}\b{TICKERS}\b",
     "a realised move of an instrument we score on"),
    (rf"\b{TICKERS}\b[^.\n]{{0,40}}\b(?:current|latest|today'?s|recent)\s+"
     r"(?:price|close|level|quote|high|low)\b",
     "an instrument's current or recent level"),
    (r"\b(?:current|latest|today'?s)\s+(?:price|close|level|quote)\s+"
     r"(?:is|was|of|at|near|around)\s+\$?\d[\d,.]*",
     "a current price, stated as a number"),
    (rf"(?:{DATE})[^.\n]{{0,60}}\b(?:{DIRECTION})\b",
     "a dated price move"),
    (rf"\b(?:{DIRECTION})\b[^.\n]{{0,60}}(?:{DATE})",
     "a dated price move"),
    # **The market as the subject, not a trade.** "Exit if the trade has lost
    # 8 points" is a stop, and was this pattern's one false positive on the
    # examples it was tuned on. "The index dropped 300 points" is not a stop.
    (rf"\b(?:{TICKERS}|index|market|futures)\b[^.\n]{{0,30}}\b(?:{DIRECTION})\s+"
     rf"(?:by\s+)?(?:about\s+|roughly\s+|nearly\s+|over\s+)?{MAGNITUDE}",
     "a realised move stated with its size"),
)
_COMPILED = tuple((re.compile(p, re.I), why) for p, why in PATTERNS)


def findings(text: str) -> list[str]:
    """Every realised-outcome statement in `text`, as '<why>: "<match>"'.

    Empty means clean. One finding per match, so a reviser is told every line
    to change at once rather than one per repair round.
    """
    out = []
    for rx, why in _COMPILED:
        for m in rx.finditer(text):
            out.append(f'{why}: "{" ".join(m.group(0).split())[:120]}"')
    # Two patterns can match one sentence; say it once.
    return list(dict.fromkeys(out))
