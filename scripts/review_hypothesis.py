#!/usr/bin/env python3
"""A reviewer for the question itself, before trials are spent on it.

    python scripts/review_hypothesis.py --hypothesis config/hypotheses/x.md
    python scripts/review_hypothesis.py --hypothesis x.md --parent y.md --json
    python scripts/review_hypothesis.py --hypothesis x.md --parent y.md --family

**Two seats, one axis each**, the discipline `audit/committee` uses for its
four. The default seat asks whether a question is worth spending trials on. The
`--family` seat asks whether a revision is still the SAME question as its
parent -- a counting question rather than a quality one, because a family
accounted for as one question that is really two inherits a correction computed
for something else, and nothing downstream can detect that.

Amendment 11 gates a machine-written hypothesis five ways and **every one of them
is mechanical**: the slug, the heading, the forbidden vocabulary, whether its
numbers are in a source, whether it is still the same family, and how long it is.
Nothing asks whether the question is worth asking.

`nq-s04-03` is why this exists. Two refinements walked an opening-range question
into something whose champion made **four trades and scored `-inf`**, so
`n_nulls` was 0 -- not one candidate reached the correction, and twenty trials
bought nothing. Every mechanical gate passed. A reader would have seen in a
minute that a strategy obeying that question could barely act.

## What it cannot do

**It cannot approve.** `VERDICTS` has no member meaning "this is a good
question", for the same reason `audit/committee` has none: a reviewer that can
approve becomes the thing that decides, and the decision here belongs to a human
and to the gate. `NO_OBJECTION` means it found no reason to stop, and that is all
it will ever be read as.

## What it is not shown

No score, no p-value, no gate verdict, nothing from validation. A reviewer that
knows which questions scored well will pass questions resembling those, and what
it is meant to protect is whether a question is worth asking at all. The same
reasoning as `refine_hypothesis.py`, and the same refusal: it will not read a
campaign's artefacts.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PROMPT = ROOT / "config" / "prompts" / "hypothesis-review.md"
FAMILY_PROMPT = ROOT / "config" / "prompts" / "family-review.md"
MODEL = "mimo-v2.6-pro"
MAX_TOKENS = 16_384
VERDICTS = ("OBJECT", "CANNOT_TELL", "NO_OBJECTION")


def context(text: str, parent: str | None = None) -> str:
    parts = []
    if parent:
        parts += ["# The question this revises\n", parent,
                  "\n\n# The revision you are reviewing\n", text]
    else:
        parts += ["# The question you are reviewing\n", text]
    parts.append(
        "\n\n# Deliberately withheld\n"
        "- every score, p-value and gate verdict\n"
        "- everything from the validation split\n\n"
        "You are judging whether this question can be answered and whether "
        "answering it would mean anything -- not whether it will score well.\n")
    return "".join(parts)


def family_context(parent: str, child: str) -> str:
    return (
        "# The parent question\n" + parent +
        "\n\n# The revision\n" + child +
        "\n\n# Deliberately withheld\n"
        "- every score, p-value and gate verdict, for the parent and the "
        "revision alike\n"
        "- everything from the validation split\n\n"
        "You are judging whether these are the same question, not whether "
        "either of them is a good one.\n")


def _normalise(out: dict, model: str, served: str, *, seat: str) -> dict:
    """The two seats refuse identically, so the refusal lives in one place."""
    verdict = str(out.get("verdict") or "").strip().upper()
    if verdict not in VERDICTS:
        # An unreadable verdict is not a pass. Same rule the committee applies:
        # no verdict is not no veto.
        return {"verdict": "CANNOT_TELL", "concerns": [], "seat": seat,
                "note": f"the reviewer returned {out.get('verdict')!r}, which is "
                        f"not one of {VERDICTS}",
                "model": model, "model_served": served, "unreadable": True}
    concerns = [str(c) for c in (out.get("concerns") or [])]
    if verdict == "OBJECT" and not concerns:
        # Objecting without saying to what is not an objection anyone can act on.
        verdict = "CANNOT_TELL"
        concerns = ["the reviewer objected without naming a concern"]
    return {"verdict": verdict, "concerns": concerns, "seat": seat,
            "note": str(out.get("note") or ""), "model": model,
            "model_served": served,
            "approval_note": (
                "NO_OBJECTION is not approval. The reviewer found no reason to "
                "stop this question; it cannot say a question is good, and "
                "nothing should read it as having done so.")}


def family_review(parent: str, child: str, *, model: str = MODEL) -> dict:
    """**A second seat, on one axis: is this still the same question?**

    Separate from `review` because the two answer different things and a VETO
    means a different thing. `review` asks whether a question is worth trials.
    This asks whether a revision is a continuation or a new pre-registration --
    a counting question, not a quality one, and the error it catches is silent:
    a family accounted for as one question that is really two inherits a
    correction computed for something else.

    It exists because the mechanical family gate in `refine_hypothesis.py` is
    two similarity floors (34% of the parent's title words, 30% of its body)
    and that file calls it the weakest of the gates in its own docstring. A
    model that drifts in small steps walks past it.
    """
    from audit.committee.llm import Client, preflight

    served = preflight(model)
    client = Client(model=model, temperature=0.0, max_tokens=MAX_TOKENS)
    out = client.ask(FAMILY_PROMPT.read_text(), family_context(parent, child))
    return _normalise(out, model, served, seat="family")


def family_findings(parent: str, child: str, *,
                    model: str = MODEL) -> list[str]:
    """Drift concerns as repair lines, empty when it did not object."""
    r = family_review(parent, child, model=model)
    if r["verdict"] != "OBJECT":
        return []
    return [f"family reviewer: {c}" for c in r["concerns"]]


def review(text: str, parent: str | None = None, *, model: str = MODEL) -> dict:
    """One call. Returns the verdict dict, normalised and safe to record."""
    from audit.committee.llm import Client, preflight

    served = preflight(model)
    client = Client(model=model, temperature=0.0, max_tokens=MAX_TOKENS)
    out = client.ask(PROMPT.read_text(), context(text, parent))
    return _normalise(out, model, served, seat="hypothesis")


def findings(text: str, parent: str | None = None, *,
             model: str = MODEL) -> list[str]:
    """The reviewer's concerns as repair lines, empty when it did not object."""
    r = review(text, parent, model=model)
    if r["verdict"] != "OBJECT":
        return []
    return [f"reviewer: {c}" for c in r["concerns"]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hypothesis", required=True, type=pathlib.Path)
    ap.add_argument("--parent", type=pathlib.Path, default=None)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--family", action="store_true",
                    help="run the FAMILY seat instead: is this revision still "
                         "the same question as --parent, or a new one? Needs "
                         "--parent, because drift is measured against it.")
    a = ap.parse_args()

    if not a.hypothesis.is_file():
        sys.exit(f"no hypothesis at {a.hypothesis}")
    parent = a.parent.read_text() if a.parent and a.parent.is_file() else None
    if a.family:
        if parent is None:
            sys.exit("--family needs --parent: drift is measured against the "
                     "question this one revises, and there is nothing to "
                     "compare a first question to")
        r = family_review(parent, a.hypothesis.read_text(), model=a.model)
    else:
        r = review(a.hypothesis.read_text(), parent, model=a.model)
    if a.json:
        print(json.dumps(r, indent=2))
        return 0
    print(f"{a.hypothesis.name}: {r['verdict']}   "
          f"(seat {r['seat']}, model {r['model_served']})")
    for c in r["concerns"]:
        print(f"  - {c}")
    if r.get("note"):
        print(f"  note: {r['note']}")
    if r["verdict"] == "NO_OBJECTION":
        print("\n  NO_OBJECTION is not approval -- it found no reason to "
              + ("call this a new question,\n  and it cannot say a revision is "
                 "a good refinement." if r["seat"] == "family" else
                 "stop this question,\n  and it cannot say a question is "
                 "good."))
    return 1 if r["verdict"] == "OBJECT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
