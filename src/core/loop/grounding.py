"""Grounding: may this sentence be said, given only the papers we froze?

The lint in `hypothesis.py` asks a different question. It asks whether the text
leaks the *gate* -- thresholds, p-values, N, the closed splits. This module asks
whether the text is *about the papers that were frozen*: does the quotation the
agent attributes to a paper actually appear in that paper.

The gap is real and it is where a draft becomes fiction. `ScreenAgent` is asked
for "the paper's own sentence the screen is keyed to" and the answer is recorded
in `ScreenVerdict.quote` without ever being compared to the paper. `DistilAgent`
writes a page from one survivor and may attribute a finding to a paper that does
not contain it. Neither is caught by the lint, because a fabricated claim about
market microstructure is made entirely of permitted words.

## Deterministic, and that is the point

The check is substring containment after whitespace normalization. No model, no
embedding, no second keep/discard. Two consequences worth stating:

**It cannot be wrong in the permissive direction.** A real quotation always
contains itself, so a genuine draft is never refused for being ungrammatical or
paraphrased in its framing -- only its quoted span is compared, and only the
span.

**It is deliberately strict elsewhere.** A quote that is *almost* the paper's
wording is refused. An abstract re-typed from memory is a fabrication for this
check's purposes, and that is the correct reading: the whole claim of the frozen
set is that the words came from one snapshot, and words reconstructed from memory
did not. The refusal message says so, so the next author can fix it by
copying.

## What is *not* checked, and why

Not entailment. Whether a sentence is *supported* by the paper it cites is a
judgment about meaning, and the NLI model that makes it is a gate next to the
gate. That is the line this repo has already drawn (`llm.py`'s model check,
`correction.py`'s fixed correction), and a second keep/discard at the drafting
stage would make the real one negotiable.

So this is a **draft check, not a campaign verdict**. It refuses text that cites
words the source does not contain. It says nothing about whether the remaining
text is any good. What it removes is the failure mode where the agent attributes
a finding to a paper that never said it, which is the one error in this family
that leaves no other trace.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Imported for type checking only. `research.py` imports this module to
    # enforce the checks, so a runtime import would close a cycle. Both types
    # are read structurally (`arxiv_id`, `title`, `abstract`, `fulltext`,
    # `papers`), which is all this module touches.
    from .research import Paper, RetrievalSet

# The minimum a quotation must be to be checked at all. Below this a match is
# an accident of common words rather than a citation of anything: "the" or "we
# show" appears in every abstract on the feed and would pass against any paper.
MIN_QUOTE_CHARS = 24

# arXiv ids as they appear in prose. Version suffix optional, since a hypothesis
# may cite `2601.10001` for a paper the set holds as `2601.10001v2`.
_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b")

# An explicit quotation. Both quote characters are accepted because the two
# agents are different models with different habits, and neither habit is wrong.
# An unterminated trailing quote is ignored: a page that ends mid-quote has a
# different problem, and this check should not also own it.
_QUOTED_RE = re.compile(r"[\"“]([^\"”\n]{%d,})[\"”]" % MIN_QUOTE_CHARS)

_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Fold the differences that are not content.

    Unicode NFKC so a curly quote or a ligature in the abstract matches its ASCII
    twin, and whitespace collapsed so a line wrap in the source is not a
    mismatch. Nothing else: case is folded by `contains`, and punctuation is left
    alone because a paper's comma is part of the words being quoted.
    """
    if not text:
        return ""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def source_text(paper: Paper) -> str:
    """Everything the paper says to us: title, abstract, any excerpt.

    The excerpt is included because it is the same paper and it is already in the
    frozen set's digest. Excluding it would refuse a quotation the agent
    legitimately read from the one part of the paper it was shown.
    """
    parts = [paper.title, paper.abstract, paper.fulltext]
    return normalize("\n".join(p for p in parts if p))


def contains(haystack: str, needle: str) -> bool:
    """Case-insensitive substring containment on normalized text.

    Case is folded here rather than in `normalize` because a caller checking a
    short id like `n` must not have its comparison widened.
    """
    h = normalize(haystack).casefold()
    n = normalize(needle).casefold()
    if not n:
        return False
    return n in h


def quoted_spans(text: str) -> tuple[str, ...]:
    """Every explicit quotation in `text`, in the order written.

    Only spans at or above `MIN_QUOTE_CHARS` are returned. A shorter one is not
    checked and, deliberately, is not an error either -- `"yes"` is not a claim
    about a paper, and refusing a draft over it would train authors to stop
    quoting.
    """
    return tuple(m.group(1).strip() for m in _QUOTED_RE.finditer(text or ""))


def cited_ids(text: str) -> tuple[str, ...]:
    """Every arXiv id the text names, base ids, first-seen order."""
    seen: dict[str, None] = {}
    for m in _ID_RE.finditer(text or ""):
        seen.setdefault(m.group(1), None)
    return tuple(seen)


class Ungrounded(RuntimeError):
    """The text claims words or papers the frozen set does not contain."""


def check_quote(quote: str, paper: Paper, *, source: str = "<screen>") -> str:
    """Assert `quote` appears in `paper`. Return the quote on success.

    This is the screen's `quote` field, which the prompt describes as the
    paper's own sentence and which nothing has ever compared to the paper. An
    empty quote is allowed: the prompt permits it, and a screen that declines to
    quote has not made a claim.
    """
    q = normalize(quote)
    if not q:
        return ""
    if len(q) < MIN_QUOTE_CHARS:
        raise Ungrounded(
            f"{source}: the quote is {len(q)} characters, under the "
            f"{MIN_QUOTE_CHARS} needed to be a citation of anything: {quote!r}. "
            "Either quote the paper's own sentence or leave the field empty; a "
            "few common words are not evidence."
        )
    if not contains(source_text(paper), q):
        raise Ungrounded(
            f"{source}: the quote does not appear in {paper.arxiv_id} "
            f"(title, abstract or the frozen excerpt).\n  quoted: {quote!r}\n"
            "A quote that is not in the paper is not a quotation. Copy the "
            "paper's wording exactly, or leave the field empty."
        )
    return quote


def check_text(text: str, papers: tuple[Paper, ...] | list[Paper], *,
               source: str = "<draft>") -> tuple[str, ...]:
    """Assert every quotation and every cited id in `text` resolves.

    Returns the ids the text cited, so a caller can record which papers a page
    leaned on. Raises on the first ungrounded claim, naming it.

    A quotation is checked against the *union* of the frozen papers rather than
    against one: a page distilling a single paper quotes that paper, and a
    research draft quoting across its whole retrieval set is quoting one of them.
    Which paper a given sentence belongs to is a judgment about meaning and is
    not made here; that each sentence exists in the set is.
    """
    corpus = tuple(papers)
    if not corpus:
        # Nothing was frozen, so nothing can be claimed about it. The retrieval
        # set is validated non-empty upstream, so reaching here means a caller
        # built an agent with no papers -- a bug worth surfacing rather than a
        # draft worth refusing.
        raise Ungrounded(
            f"{source}: there are no papers to check against. Grounding needs a "
            "frozen set; a draft checked against nothing would pass for any "
            "claim, which is the failure this module exists to catch."
        )

    corpus_text = "\n".join(source_text(p) for p in corpus)
    known = {p.arxiv_id.split("v")[0].strip() for p in corpus}

    holds = ", ".join(sorted(known)) or "(none)"
    for q in quoted_spans(text):
        if not contains(corpus_text, q):
            raise Ungrounded(
                f"{source}: a quotation is not in the frozen set.\n"
                f"  quoted: {q!r}\n"
                f"  the set holds: {holds}\n"
                "The papers may say it; this draft may only say it if the frozen "
                "text says it verbatim. Copy the source wording exactly."
            )

    cited = cited_ids(text)
    unknown = [i for i in cited if i not in known]
    if unknown:
        raise Ungrounded(
            f"{source}: cites "
            + ", ".join(unknown)
            + ", which is not in the frozen set. The set holds: "
            + ", ".join(sorted(known))
            + ". A draft may lean only on the papers that were frozen, or the "
            "retrieval has to be redone and re-committed."
        )
    return cited


def check_retrieval(text: str, retrieval: RetrievalSet | None, *,
                    source: str = "<research>") -> tuple[str, ...]:
    """`check_text` against a frozen retrieval set, or a refusal if there is none.

    The asymmetry is the point. A retrieval set is always present for a
    research draft -- that is the whole input -- so its absence is a wiring bug
    and says so, rather than silently skipping the check.
    """
    if retrieval is None:
        raise Ungrounded(
            f"{source}: no retrieval set was given, so nothing can be checked "
            "against anything. A draft without a frozen set has no papers to be "
            "about."
        )
    return check_text(text, retrieval.papers, source=source)
