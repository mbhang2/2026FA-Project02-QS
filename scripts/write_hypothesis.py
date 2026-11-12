#!/usr/bin/env python3
"""A question in, a hypothesis file out -- or a draft checked, and refined if it needs it.

    python scripts/write_hypothesis.py --question "I want a trend reversal \
        strategy that looks into CVD"
    python scripts/write_hypothesis.py --draft notes/my-cvd-idea.md
    python scripts/write_hypothesis.py --draft config/hypotheses/x.md --assess-only

**The gap it closes.** `distil_paper.py` turns a *paper* into a question, and the
only way a researcher's own question reached it was as ranking context: on
2026-11-05 "I want a trend reversal strategy that looks into CVD" went through
`retrieve.py --asking`, the closest paper won the ranking, and the file that came
out was that paper's question about small counter-trend trades. The distiller
never saw what was asked. Here the researcher's question is the subject and
papers are evidence the writer may cite.

**Three ways in.**

* `--question` -- the writer drafts the whole file.
* `--draft` -- a file the researcher wrote. It is assessed first (`assess`): the
  lint, the length, the sections a campaign needs, the grounding of its numbers,
  and the hypothesis review seat. If nothing is found it is **refined enough**
  and used as it is. Otherwise the
  writer refines it, keeping its question, into a NEW file -- the draft is never
  overwritten, because a hypothesis's commit date is its pre-registration.
* `--assess-only` -- the assessment, and nothing written.

**Every gate `distil_paper.py` has, reused rather than copied**: the forbidden-
word lint the campaign runs at registration, number grounding (against the
researcher's text, the cited papers and the prompt), the word budget, the
hypothesis review seat, and the refusal to write while a campaign is running.
Plus one it does not have: the constraints and judged requirements of
`src/core/loop/fidelity.py`, each quoting the file verbatim and validated by
`fidelity.load`, so the new question arrives with its own fidelity checks.

**The writer fixes only what the researcher fixed.** The first live run added
trading hours, a flat-by time and a one-lot limit nobody asked for, and each
became a check that refuses strategies. Every constraint must now say it came
`from` the question or the draft, and the rest are dropped and printed. A
researcher's usual limits live in `config/house-constraints.yaml` and are added
with `--house`, word for word, by this script rather than by the model.

**It stops at the file (Amendment 8).** A human reads it and commits it, and the
commit is the pre-registration. Nothing here registers a campaign. Nothing here
reads market data, so nothing here is a look.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROMPT = ROOT / "config" / "prompts" / "question-distil.md"
HOUSE = ROOT / "config" / "house-constraints.yaml"
# Where a constraint may come from. "house" is not here: house constraints are
# inserted by `add_house`, never accepted from the model.
ORIGINS = ("question", "draft")
HYPOTHESES = ROOT / "config" / "hypotheses"
REGISTRY = ROOT / "data" / "papers" / "registry.json"
# The paper distiller's model, for the same reason: a judgement task on the
# strongest authorised model. The paper picker is a ranking call and runs on the
# ranker's model.
MODEL = "mimo-v2.6-pro"
PICK_MODEL = "gemini-2.5-pro"
MAX_PAPERS = 3
MAX_CONSTRAINT_REPAIRS = 2

# What a campaign needs from a hypothesis, by heading -- matched by meaning, not
# spelling, because the 23 files on record say the same three things in a dozen
# ways ("The question" / "The claim", "yours to decide" / "yours to choose"). The
# "choose" pair is load-bearing: the agent reads it as its freedom and its
# limits, and the constraints file quotes the second.
REQUIRED_SECTIONS = {
    "the claim or question": r"^##\s+the (claim|question)\b",
    "what is yours to choose": r"^##\s+what (is )?(yours to (choose|decide)|is deliberately not fixed|you (may|can) choose)",
    "what is NOT yours to choose": r"^##\s+(what is not yours|.*not (yours|the agent's) to)",
}
RECOMMENDED_SECTIONS = {
    "why it might be true, and why not": r"^##\s+why it might",
    "what would make the answer interesting": r"^##\s+what would make",
    "what this is not asking for": r"^##\s+what this is not asking",
}


def _has(md: str, pattern: str) -> bool:
    return re.search(pattern, md, re.I | re.M) is not None


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dp = _load("distil_paper")          # the gates, reused
gr = _load("grounding")


# --------------------------------------------------------------------------- #
# assessment: is a draft refined enough?
# --------------------------------------------------------------------------- #
# A draft is held to the refiner's ceiling, not the distiller's: 1,500 words is
# what a machine-refined question may be (`refine_hypothesis.MAX_WORDS`), and a
# researcher's own file should not be refused for what ours are allowed. The
# writer still aims for 450-700; its own output is held to the distiller's.
DRAFT_MAX_WORDS = 1_500


def _draft_length(md: str) -> str:
    n = len(md.split())
    if n > DRAFT_MAX_WORDS:
        return f"{n} words, {n - DRAFT_MAX_WORDS} over the ceiling of {DRAFT_MAX_WORDS}"
    if n < dp.MIN_WORDS:
        return f"{n} words, {dp.MIN_WORDS - n} under the floor of {dp.MIN_WORDS}"
    return ""


def mechanical_findings(md: str, *sources: str, draft: bool = False) -> list[str]:
    """Everything a string check can say about a draft. Empty means clean."""
    out = []
    if not md.lstrip().startswith("#"):
        out.append("no '#' heading naming the claim on the first line")
    for name, pat in REQUIRED_SECTIONS.items():
        if not _has(md, pat):
            out.append(f"missing section: {name}")
    out += [f"gate vocabulary: {x}" for x in dp._lint_findings(md)]
    if not draft:
        # What the market did, stated as fact (`outcome_lint`, Gowtam's idea).
        # The writer's output only: a researcher's own draft is theirs to word,
        # and a human has read it. A file written from a statement may run
        # with nobody reading it (Amendment 15).
        from src.core.research.outcome_lint import findings as outcome_findings

        out += [f"realised outcome: {x}" for x in outcome_findings(md)]
    length = _draft_length(md) if draft else dp._too_long(md)
    if length:
        out.append(f"length: {length}")
    out += [f"ungrounded: {x}" for x in gr.findings(md, *sources)]
    return out


def assess(md: str, *, review: bool = True) -> dict:
    """The verdict on a draft: refined enough, or not and why.

    The draft is its own source for grounding -- a researcher's number is theirs
    to state -- so what grounding can catch here is a citation to a paper we do
    not hold. The review seat is the judgement half: whether the question is
    worth asking and answerable on this data.
    """
    found = mechanical_findings(md, md, PROMPT.read_text(), draft=True)
    advice = [f"recommended section absent: {name}"
              for name, pat in RECOMMENDED_SECTIONS.items() if not _has(md, pat)]
    concerns, status = ([], "not run (mechanical findings first)")
    if review and not found:
        concerns, status = dp._review_seat(md)
    return {"refined_enough": not found and not concerns,
            "findings": found, "concerns": concerns, "advice": advice,
            "review": status}


# --------------------------------------------------------------------------- #
# papers: evidence, chosen for the question
# --------------------------------------------------------------------------- #
def candidate_papers() -> list[dict]:
    reg = json.loads(REGISTRY.read_text())["papers"]
    return [r for r in reg.values()
            if r.get("feasibility") in ("direct", "adaptable")
            and not r.get("duplicate_of") and not r.get("not_research")]


def pick_papers(question: str, *, model: str = PICK_MODEL,
                k: int = MAX_PAPERS) -> list[dict]:
    """Up to `k` held papers that bear on the question, chosen by a model.

    Optional evidence, so a failed call returns none rather than stopping: a
    question can be written without a paper, and saying so is honest.
    """
    rows = candidate_papers()
    if not rows:
        return []
    listing = "\n".join(
        f"- {r['id']} | {str(r.get('title') or '')[:120]} | "
        f"{str(r.get('claim') or '')[:220]}" for r in rows)
    system = ("You pick research papers that bear directly on a researcher's "
              "question about one futures instrument. Return ONLY JSON: "
              '{"papers": [{"id": "...", "why": "one sentence"}]} with at most '
              f"{k} ids, best first, copied exactly from the list. Return an "
              "empty list if none bears directly on the question; a loosely "
              "related paper is worse than none.")
    user = f"# The question\n{question}\n\n# Papers we hold\n{listing}\n"
    try:
        from audit.committee.llm import Client, preflight
        preflight(model)
        got = Client(model=model, temperature=0.0, max_tokens=16_384).ask(system, user)
    except Exception as exc:                                     # noqa: BLE001
        print(f"  paper pick did not run ({type(exc).__name__}); writing "
              "without papers", flush=True)
        return []
    by_id = {r["id"]: r for r in rows}
    out = []
    for p in (got.get("papers") or [])[:k]:
        if isinstance(p, dict) and p.get("id") in by_id:
            out.append({**by_id[p["id"]], "why": str(p.get("why") or "")})
    return out


def paper_block(papers: list[dict]) -> str:
    if not papers:
        return "No paper we hold bears directly on this question. Write it without one."
    out = []
    for p in papers:
        raw = ROOT / "data" / "papers" / "raw" / f"{p['id']}.json"
        abstract = json.loads(raw.read_text()).get("abstract", "") if raw.exists() else ""
        out.append(f"### {p['id']} -- {p.get('title', '')}\n\n"
                   f"Why it may bear on the question: {p.get('why', '')}\n\n"
                   f"What we recorded it claims: {p.get('claim', '')}\n\n"
                   f"How it would adapt to our data: {p.get('adaptation', '')}\n\n"
                   f"Abstract: {abstract}\n")
    return "\n".join(out)


def sources(question: str, draft: str, papers: list[dict]) -> list[str]:
    out = [question, draft, PROMPT.read_text()]
    for p in papers:
        out += gr.sources_for(p["id"])
    return [s for s in out if s]


# --------------------------------------------------------------------------- #
# constraints: the fidelity file the new question arrives with
# --------------------------------------------------------------------------- #
def split_by_origin(items) -> tuple[list[dict], list[dict]]:
    """(kept, dropped): kept are the ones the researcher fixed, `from` removed."""
    kept, dropped = [], []
    for c in items or []:
        if not isinstance(c, dict):
            continue
        if c.get("from") in ORIGINS:
            kept.append({k: v for k, v in c.items() if k != "from"})
        else:
            dropped.append(c)
    return kept, dropped


def constraints_yaml(slug: str, out: dict, house: list[dict] | None = None) -> str:
    import yaml

    cons, _ = split_by_origin(out.get("constraints"))
    judged = [{"id": j.get("id"), "says": j.get("says")}
              for j in split_by_origin(out.get("judged"))[0]]
    have = {" ".join(str(c.get("says", "")).split()).lower() for c in cons}
    cons += [h for h in (house or [])
             if " ".join(h["says"].split()).lower() not in have]
    doc = {"schema": "constraints/1", "hypothesis": f"{slug}.md",
           "clock": "America/Chicago"}
    if cons:
        doc["constraints"] = cons
    if judged:
        doc["judged"] = judged
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=88)


def valid_items(out: dict, md: str) -> tuple[dict, list[str]]:
    """The reply with each constraint and judged requirement that `fidelity.load`
    refuses on its own removed, and why.

    **One at a time, and never a reason to rewrite the question.** The second
    live run on the CVD question offered "The instrument is NQ" as a `code_uses`
    with no identifiers. Checked as one file, that single bad item spent all
    three repair rounds, dropped every good constraint with it, and kept the
    review seat from ever reading the question. A constraint is an annotation on
    a question; a bad one is dropped, the question stands.
    """
    keep_c, keep_j, why = [], [], []
    # A requirement filed as `check: judged` is a judged requirement in the wrong
    # list -- the third CVD run did exactly this with its divergence trigger. Moved,
    # not dropped: its quote is still the question's.
    out = dict(out)
    misfiled = [c for c in out.get("constraints") or []
                if isinstance(c, dict) and c.get("check") == "judged"]
    out["constraints"] = [c for c in out.get("constraints") or [] if c not in misfiled]
    out["judged"] = list(out.get("judged") or []) + [
        {"id": c.get("id"), "says": c.get("says"), "from": c.get("from")} for c in misfiled]
    for key, keep in (("constraints", keep_c), ("judged", keep_j)):
        for item in split_by_origin(out.get(key))[0]:
            one = {"constraints": [{**item, "from": "question"}]} if key == "constraints" \
                else {"judged": [{**item, "from": "question"}]}
            bad = constraint_findings(constraints_yaml("x", one), md)
            if bad:
                why.append(f"[{item.get('id')}] {bad[0].split(': ', 1)[-1][:140]}")
            else:
                keep.append({**item, "from": "question"})
    return {**out, "constraints": keep_c, "judged": keep_j}, why


def constraint_findings(text: str, md: str) -> list[str]:
    """`fidelity.load` on the drafted file, against the drafted markdown."""
    from src.core.loop import fidelity as F

    if "constraints:" not in text and "judged:" not in text:
        return []                     # none is allowed; nothing to check
    with tempfile.TemporaryDirectory() as tmp:
        p = pathlib.Path(tmp) / "x.constraints.yaml"
        p.write_text(text)
        try:
            F.load(p, md)
        except F.ConstraintsRejected as exc:
            return [str(exc).replace(str(p), "constraints")]
    return []


def load_house(path: pathlib.Path = HOUSE) -> list[dict]:
    """The researcher's usual limits, validated with the same rules as any
    constraints file (`says` excepted: it is grounded once inserted)."""
    import yaml

    doc = yaml.safe_load(path.read_text()) or {}
    items = [dict(c) for c in doc.get("constraints") or []]
    if not items:
        raise ValueError(f"{path} lists no constraints")
    return items


def add_house(md: str, house: list[dict]) -> str:
    """Insert each house sentence into "What is NOT yours to choose", verbatim.

    Deterministic on purpose: the writer was told not to set limits, so the
    researcher's limits are added here, by string, and cannot be paraphrased
    into something the constraint's `says` no longer matches.
    """
    norm = " ".join(md.split()).lower()
    missing = [h["says"] for h in house
               if " ".join(h["says"].split()).lower() not in norm]
    if not missing:
        return md
    block = "\n".join(missing) + "\n"
    m = re.search(REQUIRED_SECTIONS["what is NOT yours to choose"], md, re.I | re.M)
    if m is None:
        return md.rstrip("\n") + "\n\n## What is NOT yours to choose\n\n" + block
    nxt = re.search(r"^## ", md[m.end():], re.M)
    at = m.end() + nxt.start() if nxt else len(md)
    head, tail = md[:at].rstrip("\n"), md[at:]
    return head + "\n\n" + block + ("\n" + tail if tail else "")


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
def context(question: str, draft: str, papers: list[dict]) -> str:
    parts = ["# What the researcher asked\n", question.strip() or "(see the draft)"]
    if draft:
        parts += ["\n\n# The researcher's draft -- refine it, keep its question\n",
                  draft]
    parts += ["\n\n# Papers you may cite\n", paper_block(papers)]
    return "".join(parts)


def _repair(found: list[str], cons: list[str]) -> str:
    items = "".join(f"- {x}\n" for x in found[:16])
    items += "".join(f"- constraints: {x}\n" for x in cons[:6])
    return ("\n\n### Your previous answer was refused\n\nFix **all** of these in "
            "one rewrite; fixing one and breaking another is how this loop "
            "fails. Keep the researcher's question.\n\n" + items)


def write(client, question: str, draft: str, papers: list[dict],
          house: list[dict] | None = None, seat=None) -> dict:
    """The distil loop, on a question: mechanical gates, then the review seat.

    `seat` replaces `dp._review_seat` (same contract) -- how `--reviewer-model`
    reaches it. Looked up at call time when None, so a test that stubs
    `dp._review_seat` stubs this too.
    """
    system = PROMPT.read_text()
    user = context(question, draft, papers)
    src = sources(question, draft, papers)
    out = client.ask(system, user)
    mech = reviews = 0
    reviewer = "NOT-REACHED (the mechanical gates were never all clean)"
    for _ in range(dp.MAX_LINT_REPAIRS + dp.MAX_REVIEW_REPAIRS):
        md = str(out.get("markdown") or "")
        slug = str(out.get("slug") or "").strip().lower()
        found = mechanical_findings(md, *src)
        if not dp.SLUG.match(slug):
            found.append(f"slug {slug!r} is not 2-6 kebab-case words")
        cons: list[str] = []          # see `valid_items`: never a reason to rewrite
        if not found:
            if reviews < dp.MAX_REVIEW_REPAIRS:
                rev, reviewer = (seat or dp._review_seat)(md)
                if rev:
                    reviews += 1
                    print(f"    reviewer objected ({reviews}/{dp.MAX_REVIEW_REPAIRS}):"
                          f" {rev[0][:100]}", flush=True)
                    out = client.ask(system, user + dp._review_note(rev))
                    continue
            elif reviews:
                reviewer += (f" -- objected {reviews}x, cap reached, taken WITHOUT "
                             "the seat clearing it")
            break
        if mech >= dp.MAX_LINT_REPAIRS:
            break
        print(f"    repair {mech + 1}: {len(found)} finding(s), "
              f"{len(cons)} constraint problem(s)", flush=True)
        out = client.ask(system, user + _repair(found, cons))
        mech += 1

    md = str(out.get("markdown") or "")
    slug = str(out.get("slug") or "").strip().lower()
    found = mechanical_findings(md, *src)
    if not dp.SLUG.match(slug):
        found.append(f"slug {slug!r} is not 2-6 kebab-case words")
    if found:
        raise ValueError("the hypothesis did not pass its gates; not written:\n  "
                         + "\n  ".join(found[:16]))
    _, dropped = split_by_origin(out.get("constraints"))
    _, dropped_j = split_by_origin(out.get("judged"))
    for c in dropped + dropped_j:
        print(f"  not the researcher's, dropped: [{c.get('id')}] "
              f"{str(c.get('says'))[:100]}", flush=True)
    if house:
        md = add_house(md, house)
        found = mechanical_findings(md, *src, *(h["says"] for h in house))
        if found:
            raise ValueError("the house constraints made the file fail its "
                             "gates:\n  " + "\n  ".join(found[:8]))
    out, bad = valid_items(out, md)
    for b in bad:
        print(f"  constraint dropped: {b}", flush=True)
    ytext = constraints_yaml(slug, out, house)
    cons = constraint_findings(ytext, md)
    if cons:
        # The question is good and its constraints are not: write the question,
        # drop the constraints, and say so. A human can add them.
        print(f"  constraints dropped: {cons[0][:160]}", flush=True)
        ytext = ""
    return {"slug": slug, "markdown": md if md.endswith("\n") else md + "\n",
            "constraints_yaml": ytext if ("constraints:" in ytext or "judged:" in ytext) else "",
            "question_as_asked": out.get("question_as_asked"),
            "adapted_claim": out.get("adapted_claim"),
            "cannot_test": out.get("cannot_test"),
            "papers_used": [p for p in (out.get("papers_used") or [])
                            if isinstance(p, str) and p in md],
            "dropped": [c.get("id") for c in dropped + dropped_j],
            "house": bool(house),
            "review": reviewer, "words": len(md.split())}


def write_files(got: dict, *, question: str, draft_path: str | None,
                papers: list[dict], model: str, served: str,
                into: pathlib.Path | None = None,
                extra: dict | None = None) -> list[pathlib.Path]:
    # Looked up at CALL time, not bound as a default: a default would freeze
    # the real folder at import, and a test (or a rehearsal) that repoints
    # HYPOTHESES would write into config/hypotheses/ anyway.
    into = into if into is not None else HYPOTHESES
    into.mkdir(parents=True, exist_ok=True)
    dest = into / f"{got['slug']}.md"
    if dest.exists():
        raise FileExistsError(f"{_shown(dest)} exists; a hypothesis is "
                              "written once. Rerun, or rename the slug.")
    dest.write_text(got["markdown"])
    written = [dest]
    if got["constraints_yaml"]:
        side = into / f"{got['slug']}.constraints.yaml"
        side.write_text(got["constraints_yaml"])
        written.append(side)
    # Where it came from, beside it and not in it: the .md is pasted into every
    # prompt of the campaign, and provenance is for the human who commits it.
    origin = into / f"{got['slug']}.origin.json"
    origin.write_text(json.dumps({
        **(extra or {}),
        "written": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "by": "scripts/write_hypothesis.py", "model": model, "model_served": served,
        "question": question, "draft": draft_path,
        "papers_offered": [p["id"] for p in papers],
        "papers_cited": got["papers_used"],
        "question_as_asked": got["question_as_asked"],
        "adapted_claim": got["adapted_claim"], "cannot_test": got["cannot_test"],
        "constraints_dropped_as_not_the_researchers": got.get("dropped", []),
        "house_constraints": got.get("house", False),
        "review": got["review"], "words": got["words"],
    }, indent=2, ensure_ascii=False) + "\n")
    written.append(origin)
    return written


# --------------------------------------------------------------------------- #
# unattended: a statement in, a committed hypothesis out (Amendment 15)
# --------------------------------------------------------------------------- #
AMENDMENT = 15


class Unattended(RuntimeError):
    """A question that cannot run unread. Says why; writes and commits nothing
    that a campaign could be registered on."""


def seat_cleared(status: str) -> bool:
    """Did the review seat READ the question and find nothing to stop?

    `_review_seat` never raises, so its status is the only record of whether
    anybody looked. A human reading the file would make CANNOT_TELL, a seat
    that never ran, or one overruled at the repair cap a judgement call. With no
    human, each is a refusal: the seat is the only reader this question gets
    before trials are spent on it.
    """
    s = status or ""
    return (s.startswith("NO_OBJECTION") and "NOT reviewed" not in s
            and "WITHOUT" not in s and "NOT-REACHED" not in s)


def _rehearsing() -> bool:
    import os
    return bool(os.environ.get("PRACTICUM_RUNS_ROOT"))


def hypotheses_dir() -> pathlib.Path:
    """`config/hypotheses/`, or the rehearsal's own folder (Amendment 13), the
    way `refine_hypothesis.HYPOTHESES` chooses: a rehearsal does not touch the
    real pre-registration."""
    import os
    root = os.environ.get("PRACTICUM_RUNS_ROOT")
    return pathlib.Path(root) / "hypotheses" if root else HYPOTHESES


def commit_message(got: dict, *, statement: str, campaign: str, model: str,
                   served: str, papers: list[dict]) -> str:
    cannot = got.get("cannot_test") or "nothing"
    return (
        f"hypothesis: {got['slug']}, written from a researcher's statement\n\n"
        f"MACHINE-WRITTEN AND MACHINE-COMMITTED under Amendment {AMENDMENT}. No "
        f"human read this before {campaign} was registered on it.\n\n"
        f"Statement, verbatim: {statement}\n"
        f"As the writer read it: {got.get('question_as_asked')}\n"
        f"What it tests: {got.get('adapted_claim')}\n"
        f"What it says it cannot test: {cannot}\n"
        f"Model: {model} (served {served})\n"
        f"Papers cited: {', '.join(got.get('papers_used') or []) or 'none'} "
        f"(offered: {', '.join(p['id'] for p in papers) or 'none'})\n\n"
        f"Gates passed: shape, gate-vocabulary lint, realised-outcome lint, "
        f"grounding, length, and a review seat that read it and did not object.\n\n"
        f"Containment (Amendment 11, unchanged): this may spend trials and may "
        f"NOT support a claim until a human has read it and said so.\n\n"
        f"Machine-Written: {campaign}\n"
        f"Hypothesis-Review: {got['review']}\n"
    )


def _shown(p: pathlib.Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:           # a rehearsal's folder may sit outside the repo
        return str(p)


def commit_files(files: list[pathlib.Path], message: str) -> str:
    """Commit exactly `files` with `message`; return the commit's sha.

    `git commit -- <paths>` commits only those paths, so whatever else the
    researcher has staged stays staged and out of a pre-registration commit.
    """
    import subprocess

    rel = [str(f.relative_to(ROOT)) for f in files]
    subprocess.run(["git", "add", "--", *rel], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message, "--", *rel],
                   cwd=ROOT, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


def write_unattended(statement: str, *, campaign: str, model: str | None = None,
                     papers: bool = True, house: bool = False,
                     reviewer_model: str | None = None) -> dict:
    """A researcher's statement in; a hypothesis written, gated and committed.

    What `--stage register --question` runs. Everything `main()` does for
    `--question`, plus what running unread requires: the review seat must have
    cleared it, the new files are committed with a `Machine-Written` trailer
    (the commit is the pre-registration; a rehearsal writes and does not
    commit), and nothing is written at all while a campaign is running.

    Returns what the campaign record keeps: the path, the commit, and what the
    statement was turned into. Raises `Unattended` naming the reason otherwise.
    """
    # `--writer-model` / `--reviewer-model`. None means the defaults: MODEL for
    # the writer, `review_hypothesis.MODEL` for the seat. They may differ, and
    # by default they do not; a seat on another model than the writer is a
    # more independent reader, which matters more when it is the only one.
    model = model or MODEL
    seat = (None if reviewer_model is None else
            (lambda md: dp._review_seat(md, model=reviewer_model)))
    statement = " ".join(statement.split())
    if not statement:
        raise Unattended("an empty statement is not a question")
    busy = dp.campaign_in_flight()
    if busy:
        raise Unattended(f"{busy} is running; a new file in config/hypotheses/ "
                         "would change its artifact fingerprint mid-flight")
    from audit.committee.llm import LLMUnavailable, ModelSubstituted

    # **A gateway failure is a refusal with a reason, not a traceback.** The
    # first live run (2026-11-11) found the writer's model being answered by
    # gemini; `preflight` refused, correctly, and the refusal escaped as a stack
    # trace from inside `register`. Same outcome -- nothing written -- but a
    # session reading the log could not tell a gateway fault from a bug.
    try:
        offered = pick_papers(statement) if papers else []
        print(f"  papers offered: {[p['id'] for p in offered] or 'none'}",
              flush=True)
        client, served = dp._client(model)
        print(f"  writing the hypothesis on {model} (served {served}) ...",
              flush=True)
        got = write(client, statement, "", offered,
                    house=load_house() if house else None, seat=seat)
    except ValueError as exc:
        raise Unattended(str(exc)) from None
    except (LLMUnavailable, ModelSubstituted) as exc:
        raise Unattended(f"the writer's model could not be used "
                         f"({type(exc).__name__}: {exc})") from None
    if not seat_cleared(got["review"]):
        raise Unattended(
            f"the review seat did not clear it ({got['review']}). With no human "
            "reading it first, the seat is its only reader; write it with "
            "scripts/write_hypothesis.py --question, read it, and register on "
            "the file instead")
    files = write_files(
        got, question=statement, draft_path=None, papers=offered, model=model,
        served=served, into=hypotheses_dir(),
        extra={"machine_written": {"amendment": AMENDMENT, "campaign": campaign,
                                   "read_by_a_human": False}})
    commit = None
    if _rehearsing():
        print("  REHEARSAL: written but NOT committed (Amendment 13).", flush=True)
    else:
        commit = commit_files(files, commit_message(
            got, statement=statement, campaign=campaign, model=model,
            served=served, papers=offered))
        print(f"  committed {commit[:10]}, marked `Machine-Written: {campaign}`",
              flush=True)
    for f in files:
        print(f"  wrote {_shown(f)}", flush=True)
    print(f"  asked:   {got['question_as_asked']}", flush=True)
    print(f"  testing: {got['adapted_claim']}", flush=True)
    if got["cannot_test"]:
        print(f"  CANNOT TEST: {got['cannot_test']}", flush=True)
    return {"path": files[0], "commit": commit,
            "origin": {"machine_written": True, "amendment": AMENDMENT,
                       "statement": statement,
                       "question_as_asked": got["question_as_asked"],
                       "adapted_claim": got["adapted_claim"],
                       "cannot_test": got["cannot_test"],
                       "writer_model": model, "writer_served": served,
                       "reviewer_model": reviewer_model or "default",
                       "review": got["review"], "commit": commit,
                       "read_by_a_human": False}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--question", default="", help="what you want to investigate")
    ap.add_argument("--draft", type=pathlib.Path, default=None,
                    help="your own hypothesis file; assessed, and refined only if "
                         "it needs it")
    ap.add_argument("--assess-only", action="store_true")
    ap.add_argument("--no-papers", action="store_true",
                    help="write from the question alone")
    ap.add_argument("--refine", action="store_true",
                    help="refine the draft even if it is refined enough")
    ap.add_argument("--house", action="store_true",
                    help="add the usual limits in config/house-constraints.yaml, "
                         "word for word")
    ap.add_argument("--model", default=MODEL)
    a = ap.parse_args()
    if not a.question and not a.draft:
        ap.error("give --question, --draft, or both")

    draft = a.draft.read_text() if a.draft else ""
    if a.draft:
        print(f"assessing {a.draft}", flush=True)
        verdict = assess(draft)
        for f in verdict["findings"]:
            print(f"  finding  {f}")
        for c in verdict["concerns"]:
            print(f"  review   {c}")
        for x in verdict["advice"]:
            print(f"  advice   {x}")
        print(f"  review seat: {verdict['review']}")
        if verdict["refined_enough"] and not a.refine:
            print("  REFINED ENOUGH -- use it as it is. Commit it, then register.")
            return 0
        if a.assess_only:
            print("  NOT refined enough. Rerun without --assess-only to refine it.")
            return 1

    if not a.assess_only:
        busy = dp.campaign_in_flight()
        if busy:
            sys.exit(f"{busy} is running; a new file in config/hypotheses/ would "
                     "change its artifact fingerprint mid-flight. Wait for it.")
    question = a.question or ""
    papers = [] if a.no_papers else pick_papers(question or draft[:4000])
    print(f"papers offered: {[p['id'] for p in papers] or 'none'}", flush=True)
    client, served = dp._client(a.model)
    print(f"writing on {a.model} (served {served}) ...", flush=True)
    got = write(client, question, draft, papers,
                house=load_house() if a.house else None)
    files = write_files(got, question=question,
                        draft_path=str(a.draft) if a.draft else None,
                        papers=papers, model=a.model, served=served)
    for f in files:
        print(f"  wrote {f.relative_to(ROOT)}")
    print(f"  {got['words']} words; review seat: {got['review']}")
    print(f"  asked:   {got['question_as_asked']}")
    print(f"  testing: {got['adapted_claim']}")
    if got["cannot_test"]:
        print(f"  cannot test: {got['cannot_test']}")
    print("\nNOT registered. Read it, commit it (Amendment 8: the commit is the "
          "pre-registration), then:\n  python scripts/nq_campaign.py --stage "
          f"register --name <id> --hypothesis config/hypotheses/{got['slug']}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
