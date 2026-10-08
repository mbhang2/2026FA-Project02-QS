#!/usr/bin/env python3
"""Is every quantity in a distilled claim actually in its sources?

    python scripts/grounding.py --claim config/hypotheses/x.md --paper 2605.04004v3

**Why this exists, with the defect that motivated it.** Stage 3 distils a paper
into a hypothesis. The lint (`src.core.loop.hypothesis`) checks the file says
nothing about the gate. Nothing checked that it says anything *true*.

The first real distillation, of `2605.04004v3` on 2026-09-28:

| | |
|---|---|
| the paper's abstract | "gross return before friction is **0.07-1.50**" |
| the distilled hypothesis | "gross returns in the **0.5-1.5** point range" |

A lower bound off by a factor of seven, stated as fact in what would have been a
pre-registered research question, and invisible to the lint because no gate
vocabulary is involved. A campaign registered on that file would have asked a
question whose premise was wrong, and the paper it cited would have been the
evidence for it.

**The idea is Bryan Rodriguez's** `aider-validate`
(`origin/feature/rag-autoresearch`): given a claim document and a source
document, report whether the claims are grounded, and exit non-zero when a quote
is fabricated. His runs a local embedding database and an entailment model. This
is the deterministic core of the same idea, and it is deliberately not a model:

- it needs no server, no embeddings, no network, and no API key;
- it cannot hallucinate, so its findings need no second check;
- and `docs/design/architecture.md`'s rule applies — make the bug
  unrepresentable rather than ask a model whether it happened.

It catches less than his does. It does not judge whether a *sentence* is
entailed, only whether the quantities and quoted spans are present. That is the
subset that is checkable without judgement, and it is the subset the observed
defect lives in.

## What counts as grounded

A number is grounded if it appears in **any** source, compared **numerically**
rather than textually, so `2.0` matches `2`, and `1.50` matches `1.5`. The
sources are the paper *and the prompt the model was given* — the prompt states
what our data is (session counts, bar intervals), so a number it supplied is one
the model was entitled to use. That rule needs no hand-maintained allowlist of
"our" constants, which would rot.

A quoted span is grounded if it appears in a source after whitespace and quote
normalisation.

## What it does not do

It does not decide whether an ungrounded number is *wrong*. `543` may be correct
and merely unstated in the sources. It reports; the caller decides. In
`distil_paper.py` the caller feeds the findings back to the model exactly as it
feeds back lint findings, and refuses to write the file if any survive — so the
machine never writes a quantity it cannot point at.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A number, with optional thousands separators and an optional decimal part.
# Percent signs and units are left out of the match: the quantity is the thing
# being checked, not its formatting.
NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\w])")

# **An arXiv identifier is a citation, not a quantity.** `2605.04004v3` would
# otherwise be read as the number 2605 and reported ungrounded on every file that
# cites its source correctly -- punishing the one thing stage 3 requires. Stripped
# from the claim and from the sources before any number is extracted.
PAPER_ID = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b")


# Straight and curly double quotes, and the span between them. Single quotes are
# deliberately excluded -- apostrophes would make every possessive a "quote".
QUOTED = re.compile(r"[\"“]([^\"”\n]{12,240})[\"”]")

# **A range is checked as a unit, and this is the part that survives full text.**
# Presence-grounding weakens as the source grows: in a 44,000-character paper
# almost any small decimal appears somewhere, so `0.5` reads as grounded even
# though the paper's range is `0.07-1.50`. The observed defect was a misquoted
# RANGE, and a range is a pair -- so the pair must appear as a pair. `0.07-1.50`
# is in the source; `0.5-1.5` is not, wherever its halves occur.
RANGE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)(?![\w])")


# Below this, a number is structural rather than a claim: "one position at a
# time", "the first 30 minutes", a section number. Requiring a source for every
# `1` and `2` in English prose produces noise and no signal.
CLAIM_MIN = 10.0


def _strip_identifiers(text: str) -> str:
    """Remove every paper identifier before numbers are read out of a text.

    **A DOI is not a quantity either.** `PAPER_ID` has stripped arXiv ids since
    the first distillation, for exactly this reason: `2605.04004v3` would be read
    as 2605 and reported ungrounded on every file that cites its source
    correctly. When the corpus stopped being arXiv-only this acquired a second
    shape and the stripping did not follow -- so a hypothesis citing
    `doi:10.2139/ssrn.7124578` had `10.2139` pulled out of the DOI and demanded
    of the sources. Found 2026-10-04 on the first non-arXiv distillation, which
    it refused; it would have refused every one of them.
    """
    out = PAPER_ID.sub(" ", DOI_ID.sub(" ", text))
    # **And the registry's OWN keys**, which is the case the two shape patterns
    # above still missed. Our key for an SSRN paper is
    # `doi-10.2139_ssrn.7124578`; a model citing it in that form, or as a bare
    # "SSRN 7124578", leaves `7124578` behind to be demanded of the sources.
    # Matched longest-first so a key is never half-removed.
    for pid in sorted(registry_ids(), key=len, reverse=True):
        if len(pid) >= 6 and pid in out:
            out = out.replace(pid, " ")
    return out


def numbers(text: str) -> set[float]:
    """Every numeric literal, as floats so formatting cannot cause a mismatch."""
    out: set[float] = set()
    for whole, frac in NUMBER.findall(_strip_identifiers(text)):
        try:
            out.add(float(whole.replace(",", "") + ("." + frac if frac else "")))
        except ValueError:                                       # pragma: no cover
            continue
    return out


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("’", "'").split())


def ungrounded_numbers(claim: str, *sources: str) -> list[str]:
    """Numbers in `claim` that appear in no source. Reported as written.

    A decimal is always a claim. An integer is a claim only at or above
    `CLAIM_MIN`, and a four-digit year is never one -- a hypothesis may date
    itself, and the date is not the paper's to support.
    """
    have = set()
    for s in sources:
        have |= numbers(s)
    out: list[str] = []
    seen: set[float] = set()
    # `_strip_identifiers`, not `PAPER_ID` alone. This was the second stripping
    # site and it is why the first fix did not take: an identifier has to stop
    # being a number in both places or in neither.
    for whole, frac in NUMBER.findall(_strip_identifiers(claim)):
        raw = whole + ("." + frac if frac else "")
        try:
            val = float(whole.replace(",", "") + ("." + frac if frac else ""))
        except ValueError:                                       # pragma: no cover
            continue
        if val in seen or val in have:
            continue
        # **Quoting a figure to fewer decimals is grounded.** A source holding
        # 652.2132352941182 supports a claim of "652.213"; comparing raw floats
        # calls that invented. Measured 2026-10-01: a refinement was refused for
        # seven numbers, five of which were correctly-rounded source figures.
        #
        # This does not loosen the check. A claim still has to match a real
        # source number AT ITS OWN STATED PRECISION, so 578.5 against a source
        # with no 578.5 is still ungrounded -- which is how the same run's two
        # genuinely invented numbers were caught.
        if frac and any(round(h, len(frac)) == val for h in have):
            continue
        is_decimal = bool(frac)
        is_year = not is_decimal and 1900 <= val <= 2100
        if is_year:
            continue
        if not is_decimal and val < CLAIM_MIN:
            continue
        seen.add(val)
        out.append(raw)
    return out


def _ranges(text: str) -> set[tuple[float, float]]:
    out: set[tuple[float, float]] = set()
    for lo, hi in RANGE.findall(_strip_identifiers(text)):
        try:
            a, b = float(lo), float(hi)
        except ValueError:                                       # pragma: no cover
            continue
        if a < b:                    # "5-3" is a citation or a date, not a range
            out.add((a, b))
    return out


def ungrounded_ranges(claim: str, *sources: str) -> list[str]:
    """Ranges in `claim` whose PAIR appears in no source.

    A range whose halves are each present somewhere is not a grounded range:
    `0.5-1.5` against a paper saying `0.07-1.50` is exactly the defect this
    module was built from, and it survives full-text grounding only as a pair.
    """
    have: set[tuple[float, float]] = set()
    for s in sources:
        have |= _ranges(s)
    out: list[str] = []
    seen: set[tuple[float, float]] = set()
    for lo, hi in RANGE.findall(_strip_identifiers(claim)):
        try:
            pair = (float(lo), float(hi))
        except ValueError:                                       # pragma: no cover
            continue
        if pair[0] >= pair[1] or pair in seen or pair in have:
            continue
        # A range of two small integers is prose ("3 to 5 bars"), not a claim.
        if pair[1] < CLAIM_MIN and "." not in lo and "." not in hi:
            continue
        seen.add(pair)
        out.append(f"{lo}-{hi}")
    return out


# **Sentence punctuation inside the closing quote is not part of the quote.**
# American convention puts the period inside: `"... Falsification Study."` The
# paper's title has no trailing period, so the first real distillation was
# refused for quoting its own source correctly. Stripped from both ends before
# matching -- this is punctuation, not a claim.
_EDGE_PUNCT = ".,;:!?—–- \t"


def ungrounded_quotes(claim: str, *sources: str) -> list[str]:
    """Quoted spans in `claim` that appear in no source."""
    have = [_norm(s) for s in sources]
    out: list[str] = []
    for span in QUOTED.findall(claim):
        n = _norm(span).strip(_EDGE_PUNCT)
        if not n:
            continue
        if not any(n in h for h in have):
            # **Not truncated.** A report that hides the span cannot be acted on:
            # the first real failure was a quoted paper title that matched when
            # tested by hand, and the 90-character cut hid whatever differed.
            # The same diagnostics gap cost an hour on the proposer's
            # `finish_reason` earlier the same day.
            out.append(span)
    return out


# **A citation is a claim that a paper exists, and nothing checked it.** The
# grounding check strips arXiv ids before looking at numbers, so a cited id was
# never examined at all -- a hypothesis could name a paper nobody has ever read
# and pass every gate. Six committed hypotheses now carry citations.
#
# The rule is **registry membership, not plausibility**. The refiner is only ever
# SHOWN papers from our registry; an id outside it was either invented or lifted
# from some paper's bibliography, and neither has been fetched, screened or
# ranked here. "It looks like a real arXiv id" is not the property we want.
#
# What counts as a citation at all matters, because P&L figures share the shape:
# a refinement written on 2026-10-02 contains "net 1133.4125 index points", which
# a naive id-matcher reads as arXiv 1133.4125. So a bare number is a citation
# only when the registry confirms it; otherwise it needs an explicit marker --
# an `arXiv:` prefix or a version suffix -- and then it must still resolve.
CITATION_MARKED = re.compile(r"(?:arXiv:|arxiv:)\s*(\d{4}\.\d{4,5})(?:v\d+)?"
                             r"|\b(\d{4}\.\d{4,5})v\d+\b")

# **DOIs and OpenAlex ids, since the corpus stopped being arXiv-only.** A
# citation check that only understands `NNNN.NNNNN` would wave through
# `doi:10.1016/j.jbankfin.2099.999999` -- an invented journal paper, which is the
# easier thing to invent than an invented arXiv id, because the shape is less
# familiar. `10.` plus a registrant code is unambiguous enough to treat as a
# citation whether or not it is marked, unlike a bare arXiv-shaped number, which
# can be a quantity.
# **Trailing delimiters are not part of the identifier.** The character class
# excluded whitespace and a few brackets but not a backtick, so a DOI written in
# markdown code -- `10.2139/ssrn.7364204`, which is how anyone naturally writes
# one -- captured the closing backtick, missed the registry, and was refused as
# invented. Caught 2026-10-07 by the pre-flight rehearsal on a paper the registry
# actually holds. The trailing strip is belt and braces for the delimiters nobody
# has thought of yet.
DOI_ID = re.compile(r"\b(10\.\d{4,9}/[^\s\]\)\}>,;\"'`]+)")


def _clean_doi(d: str) -> str:
    return d.rstrip(".,;:'\"`)]}>")
OPENALEX_ID = re.compile(r"\b([Ww]\d{6,12})\b")


def registry_ids(registry: "dict | None" = None) -> set[str]:
    """Every paper id we hold, with and without its version suffix."""
    if registry is None:
        f = ROOT / "data" / "papers" / "registry.json"
        if not f.exists():
            return set()
        registry = json.loads(f.read_text())["papers"]
    out: set[str] = set()
    for pid, row in (registry.items() if hasattr(registry, "items")
                     else ((k, {}) for k in registry)):
        out.add(pid)
        out.add(pid.split("v")[0])
        # The row's own identifiers, so a hypothesis may cite the DOI or the
        # OpenAlex id rather than our internal key -- which is what anyone
        # reading the paper would naturally write.
        if isinstance(row, dict):
            doi = (row.get("doi") or "").lower().replace("https://doi.org/", "")
            if doi:
                out.add(doi)
            if row.get("openalex_id"):
                out.add(str(row["openalex_id"]))
    return out


def citations(claim: str, known: "set[str] | None" = None) -> set[str]:
    """Ids the text is actually claiming as papers.

    Marked ones always count. A bare `NNNN.NNNNN` counts only when the registry
    recognises it -- otherwise it is a quantity, and quantities are the number
    check's business.
    """
    known = registry_ids() if known is None else known
    found = {a or b for a, b in CITATION_MARKED.findall(claim)}
    for m in PAPER_ID.findall(claim):
        bare = m.split("v")[0]
        if bare in known:
            found.add(bare)
    # A DOI is a citation on sight: the shape cannot be a quantity.
    out = {c.split("v")[0] for c in found}
    out |= {_clean_doi(d).lower() for d in DOI_ID.findall(claim)}
    # An OpenAlex id only counts when we hold it -- `W` plus digits is too thin
    # a shape to assume, and a lone "W2252579905" in prose is unlikely but a
    # false positive here would refuse a clean file.
    out |= {w for w in OPENALEX_ID.findall(claim) if w in known}
    return out


def unverifiable_citations(claim: str, known: "set[str] | None" = None) -> list[str]:
    """Cited ids that are in no registry we hold. Non-empty means refuse."""
    known = registry_ids() if known is None else known
    lower = {k.lower() for k in known}
    return sorted(c for c in citations(claim, known)
                  if c not in known and c.lower() not in lower)


def findings(claim: str, *sources: str) -> list[str]:
    """Readable lines, empty when everything checks out."""
    out = [f"the range {r!r} appears in no source as a range"
           for r in ungrounded_ranges(claim, *sources)]
    out += [f"{n!r} appears in no source" for n in ungrounded_numbers(claim, *sources)]
    out += [f"quoted {q!r} appears in no source" for q in ungrounded_quotes(claim, *sources)]
    out += [f"cites {c!r}, which is in no registry we hold -- a paper nobody "
            f"has fetched, screened or ranked here" for c in
            unverifiable_citations(claim)]
    return out


def sources_for(paper_id: str) -> list[str]:
    """The paper as the registry holds it, plus the distil prompt.

    The prompt is a source because it is what told the model what our data is;
    a number it supplied is one the model was entitled to write down.
    """
    reg = json.loads((ROOT / "data" / "papers" / "registry.json").read_text())
    row = reg["papers"].get(paper_id)
    if row is None:
        raise KeyError(f"{paper_id} is not in the registry")
    raw = ROOT / "data" / "papers" / "raw" / f"{paper_id}.json"
    paper = json.loads(raw.read_text()) if raw.exists() else {}
    # **The whole paper when we have it.** Grounding against an abstract makes
    # every figure from the body read as unsupported, which is a check that
    # punishes the faithful distillation and passes the vague one. `stage 0b`
    # (`scripts/fetch_fulltext.py`) writes these; 20-245x the abstract.
    full = ROOT / "data" / "papers" / "text" / f"{paper_id}.txt"
    parts = [full.read_text() if full.exists() else "",
             str(paper.get("abstract") or ""), str(paper.get("title") or ""),
             str(row.get("title") or ""), str(row.get("claim") or ""),
             str(row.get("adaptation") or ""),
             str(row.get("feasibility_reason") or ""),
             (ROOT / "config" / "prompts" / "paper-distil.md").read_text()]
    return [p for p in parts if p]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--claim", required=True, type=pathlib.Path,
                    help="the file whose quantities are being checked")
    ap.add_argument("--paper", required=True, help="registry id of the source paper")
    a = ap.parse_args()

    if not a.claim.exists():
        sys.exit(f"no such file: {a.claim}")
    found = findings(a.claim.read_text(), *sources_for(a.paper))
    if not found:
        print(f"{a.claim}: every quantity and quoted span is in the sources")
        return 0
    print(f"{a.claim}: {len(found)} ungrounded:")
    for f in found:
        print(f"  {f}")
    print("\nA number in no source is not necessarily wrong -- it may be right and "
          "merely unstated. It is not something a machine should have written down "
          "unchecked.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
