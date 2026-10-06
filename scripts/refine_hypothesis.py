#!/usr/bin/env python3
"""Hypothesis k+1, from hypothesis k and what campaign k actually did.

    python scripts/refine_hypothesis.py --campaign nq-s01-continuation --dry-run
    python scripts/refine_hypothesis.py --campaign nq-s01-continuation --commit

This is the L1.5 the pre-registration anticipated: the step that makes an
overnight session a *search* rather than a list of questions written in advance.
Governed by **Amendment 11**, which is what lets it commit without a human
reading the result first, and which states plainly what that costs.

## What it is shown, and the one thing it is not

It is shown the parent question, **what the strategy did**, and **what the four
auditors objected to**. It is not shown the gate's p-value, the gate's verdict,
or the campaign's best score.

That is deliberate and it is the most important decision in this file. Task 36
measured an LLM shown its own prior scores learning to improve them -- on a
dataset with no edge in it, trend p = 0.0031, +1.59 SD over a history-withheld
control. A refiner shown the gate's output is being taught to write questions
that score well. A refiner shown turnover, concentration and four auditors'
objections is being taught to write questions that survive scrutiny. Only one of
those is the thing we want.

## Validation is unreadable, by construction

Section 4: campaign k+1 may depend on prior campaigns' **train** results, never
on their validation results. That is not enforced here by instruction. This
script **refuses to run** if the campaign it was given has any validation
artefact on disk, because the honest reading of "may not depend on" is that the
information must not be in the room.

This works because `scripts/research_session.py` validates at the stop: during
phase 1 no validation file exists for any campaign, so the refiner runs, and
after phase 2 it will not run at all. The ordering enforces the rule.

## The gates of Amendment 11

**Six mechanical, then two agentic.** Nothing is written unless all of them are
satisfied.

Mechanical, in the repair loop, in this order because each is free and the next
one costs more: the shape (a 2-6 word slug and a `#` heading), the lint (it must
not name how it will be judged), grounding (every number in a source), the
family unchanged, the length, and no validation artefact readable. A recorded
diff against the parent goes in the commit.

Agentic, and only on a draft that cleared all six, because spending a model call
to be told what a regex already knew is a wasted minute:

  * the **hypothesis** seat -- is this question worth spending twenty trials on?
  * the **family** seat -- is this revision still the same question as its
    parent, or a new one?

Two seats rather than one concern inside one seat, which is the discipline
`audit/committee` uses for its four: a VETO means a different thing on each
axis. Neither can approve. Both fail open on their own error -- a dead model
call must not kill an overnight session -- and because they fail open, what each
one did is written into the commit trailer, so `git log` answers whether a
question was ever actually read.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import prereg  # noqa: E402

# Amendment 13: `runs/` unless a rehearsal redirects it. A rehearsal
# writes under `.scratch/`, which `runlog.ledger_paths()` does not read,
# so its trials are invisible to `global_n()` by construction.
import os as _os
_RUNS = pathlib.Path(_os.environ.get("PRACTICUM_RUNS_ROOT") or (ROOT / "runs"))
CAMPAIGNS = _RUNS / "campaigns"
# **A rehearsal writes its questions under its own root, never into
# `config/hypotheses/`.** Amendment 13: a rehearsal must not mutate the real
# pre-registration directory. The first one did -- it machine-committed
# `outsized-move-continuation.md` into the repo from a run against synthetic
# data, a question no human had read and that says nothing about the market.
# Stated limit of the rehearsal: it therefore does not exercise the real commit
# path, only everything up to it.
HYPOTHESES = (_RUNS / "hypotheses") if _os.environ.get("PRACTICUM_RUNS_ROOT") \
    else (ROOT / "config" / "hypotheses")
PROMPT = ROOT / "config" / "prompts" / "hypothesis-refine.md"
REGISTRY = ROOT / "data" / "papers" / "registry.json"

# **mimo-v2.6-pro: the smartest model we are allowed, where one call does the
# thinking.** Measured 2026-10-03: 738 s per call on a cache miss, which is
# ruinous for the proposer (20 calls a campaign, 5.4 h instead of 1.4 h) and
# cheap here, because a session refines two or three times. Allocation by call
# count, not by smartness alone.
#
# It is also CACHED: an identical prompt returns byte-identical output in 0.3 s
# ignoring temperature. Harmless here -- every repair attempt appends to the
# prompt, so each is a fresh key -- but it is why the sub-second entries in
# docs/setup/MODEL-ACCESS.md are marked suspect.
MODEL = "mimo-v2.6-pro"
REFINE_MAX_TOKENS = 32_768
MIN_WORDS = 350
# **1,500 words, raised from 1,125 on 2026-10-03.** The old number was 1,500
# *tokens* expressed in words, which was the top of `paper-retrieval.md`'s stated
# budget. Michael asked why not 1,500 words, and the budget's own reasons do not
# forbid it: that document says in as many words that cost "was never the binding
# constraint" and names latency and context dilution instead. Measured against the
# real proposal prompt, now 167,489 characters:
#
#     the doc's worked example (nq-orderflow-02)   1,120 words   3.6% of prompt
#     old cap                                      1,125 words   3.6%
#     new cap                                      1,500 words   4.8%
#     the pathology the budget was written against 11,475 words 36.5%
#
# So this moves the hypothesis from 3.6% to 4.8% of the prompt, nowhere near the
# third-of-the-prompt case the budget exists to prevent, and the latency of 650
# extra input tokens is lost in a 10-80 s call.
#
# What the cap was really doing was forcing a refinement to DISPLACE rather than
# accrete, and a looser cap weakens that. The prompt carries that pressure
# explicitly instead, because it is the honest place for it.
#
# **`distil_paper.py` keeps 1,125 and that asymmetry is deliberate.** A test
# there argues a ceiling exists so the NEXT question is smaller than the
# worst legacy file (1,422 words), which is right for turning a paper into a
# new question. A revision is a different operation: it carries its parent,
# the evidence, and what it took from the literature, and at 1,125 a 920-word
# parent left ~200 words of headroom -- which is what ended session-04 after
# one campaign of three.
MAX_WORDS = 1_500
MAX_LINT_REPAIRS = 8
# **After this many failed repairs, ask a different model.** A model that has
# missed the same gate five times is not about to comply on the sixth, and
# session-04 died with one campaign of three because nothing escalated. The
# fallback is a different family from the proposer on purpose.
SWITCH_MODEL_AFTER = 3
FALLBACK_MODEL = "gemini-2.5-pro"
MAX_FORMAT_RETRIES = 3
# The reviewer is a judgement, not a defect check, so it gets fewer rounds than
# the mechanical gates: two exchanges is a revision, more is an argument.
MAX_REVIEW_REPAIRS = 2
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){1,5}$")
PAPER_CHARS = 6_000
MAX_PAPERS = 3


# --------------------------------------------------------------------------- #
# gate 5: validation must not be in the room
# --------------------------------------------------------------------------- #
def validation_readable(campaign: str) -> list[str]:
    """Any validation artefact on this campaign. Non-empty means refuse."""
    d = CAMPAIGNS / campaign

    def rel(x: pathlib.Path) -> str:
        try:
            return str(x.relative_to(ROOT))
        except ValueError:
            return str(x.relative_to(d.parent))   # tmp dirs in the tests

    return sorted(rel(x) for x in d.glob("validation*") if x.is_file())


# --------------------------------------------------------------------------- #
# what the refiner is shown
# --------------------------------------------------------------------------- #
def _round(v, places: int = 2):
    """Round a diagnostic for display, through dicts and lists."""
    if isinstance(v, dict):
        return {k: _round(x, places) for k, x in v.items()}
    if isinstance(v, list):
        return [_round(x, places) for x in v]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    return round(v, places)


def evidence(campaign: str) -> dict:
    """What the strategy DID and what the auditors said. Never the gate's score.

    `redacted` is returned alongside so the record shows what was withheld rather
    than leaving a reader to infer it.
    """
    d = CAMPAIGNS / campaign
    summary = json.loads((d / "tier2-summary.json").read_text())
    best = (summary.get("best_candidate") or {})
    score = dict(best.get("score") or {})
    diags = dict(best.get("diagnostics") or {})

    # Withheld on purpose -- see the module docstring.
    for k in ("primary", "primary_uncharged", "sharpe", "sortino",
              "significance", "rejected", "reject_reason"):
        score.pop(k, None)

    reasons = []
    cv = d / "committee-verdict.json"
    if cv.exists():
        v = json.loads(cv.read_text())
        for a in v.get("auditors") or []:
            reasons.append({"axis": a.get("axis"), "verdict": a.get("verdict"),
                            "reasons": a.get("reasons") or []})

    n = summary.get("n") or {}
    return {
        "campaign": campaign,
        "what_the_strategy_did": {
            "trades": score.get("trade_count"),
            "sessions_scored": score.get("n_sessions"),
            "downside_sessions": score.get("downside_sessions"),
            "net_pnl_index_points": score.get("net_pnl"),
            "skew": score.get("skew"), "kurtosis": score.get("kurtosis"),
        },
        # **Rounded, so the numbers it sees are numbers it can quote.** The raw
        # diagnostics carry full float precision (652.2132352941182), and a model
        # that sensibly rounds one then fails the grounding check against the
        # unrounded source. Fixing the check was half of it; not handing it
        # fifteen significant figures of a P&L total is the other half.
        "concentration": {k: _round(diags.get(k)) for k in
                          ("by_month", "by_hour_utc", "by_weekday",
                           "best_session_share", "avg_holding_period_ns")
                          if diags.get(k) is not None},
        "auditors": reasons,
        "candidates_tried": (n.get("trials") if isinstance(n, dict) else n),
        "redacted": ["the gate's p-value", "the gate's verdict",
                     "every candidate's score, including the best"],
    }


def papers_for(parent_text: str = "", limit: int = MAX_PAPERS) -> list[dict]:
    """Papers worth showing the refiner, best first, with the ranker's judgement.

    Two things were wrong with the first version and both killed the impact.

    **It chose globally.** `rows[:limit]` took the first three in registry order
    among everything rated high on either axis -- no reference to the question
    being refined. Three papers about evaluation protocol handed to a refiner
    rewriting a market question.

    **It shipped raw body text and nothing else.** 6,000 characters of a paper
    with no framing. The ranker had already written `acts_on` (what a strategy
    would do differently) and `adaptation` (how it maps to our data), and those
    are the useful part; the body is for checking a claim, not for reading cold.

    Ordering is by the campaign axis first, because that axis asks whether there
    is something to act on, then by overlap with the question's own vocabulary.
    """
    if not REGISTRY.exists():
        return []
    rank = {"high": 0, "medium": 1, "low": 2, None: 3}
    rows = [v for v in json.loads(REGISTRY.read_text())["papers"].values()
            if v.get("feasibility") in ("direct", "adaptable")
            and (v.get("relevance") in ("high", "medium")
                 or v.get("gate_relevance") == "high")]

    # Overlap with the question's own words -- crude on purpose. A paper about
    # order flow handed to a question about order flow beats one about drawdown
    # quantiles, and nothing here needs an embedding model to see that.
    stop = {"the", "and", "that", "this", "with", "from", "what", "does", "for",
            "are", "its", "not", "but", "has", "was", "which", "would", "than",
            "paper", "data", "session", "sessions", "question", "strategy"}
    want = {w for w in re.findall(r"[a-z]{4,}", parent_text.lower())
            if w not in stop}

    def overlap(v: dict) -> int:
        text = " ".join(str(v.get(k) or "") for k in
                        ("title", "relevance_reason", "acts_on", "adaptation",
                         "speaks_to_deferral")).lower()
        return -len(want & set(re.findall(r"[a-z]{4,}", text)))

    rows.sort(key=lambda v: (rank[v.get("relevance")], overlap(v),
                             rank[v.get("gate_relevance")]))
    out = []
    for v in rows[:limit]:
        body = ""
        f = ROOT / "data" / "papers" / "text" / f"{v['id']}.txt"
        if f.exists():
            body = f.read_text()[:PAPER_CHARS]
        out.append({"id": v["id"], "title": v.get("title"),
                    "campaign_axis": v.get("relevance"),
                    "gate_axis": v.get("gate_relevance"),
                    # the ranker's own words: what to DO with this paper
                    "acts_on": v.get("acts_on"),
                    "adaptation": v.get("adaptation"),
                    "speaks_to_deferral": v.get("speaks_to_deferral"),
                    "abstract": (v.get("abstract") or "")[:1_500],
                    "body": body})
    return out


def context(parent_text: str, ev: dict, papers: list[dict]) -> str:
    parts = ["# The question as it stands\n", parent_text,
             "\n\n# What a strategy written against it actually did\n",
             json.dumps({k: ev[k] for k in
                         ("what_the_strategy_did", "concentration",
                          "candidates_tried")}, indent=2, default=str),
             "\n\n# What the auditors objected to\n"]
    for a in ev["auditors"]:
        parts.append(f"\n## {a['axis']} -- {a['verdict']}\n")
        for r in a["reasons"]:
            parts.append(f"- {r}\n")
        if not a["reasons"]:
            parts.append("- (no objection recorded)\n")
    parts.append("\n\n# Deliberately withheld from you\n")
    for r in ev["redacted"]:
        parts.append(f"- {r}\n")
    parts.append(
        "\nYou are not told how the study scored this, because a question "
        "written to score well is not the same as a question that is true.\n")
    if papers:
        parts.append(
            "\n\n# Papers judged relevant, with what each one would have a "
            "strategy do differently\n\nFor each: is there an idea here usable "
            "on its own, or that could be ADDED to the question above? Cite the "
            "arXiv id if you use one; say which you considered and rejected if "
            "you do not.\n")
        for p in papers:
            parts.append(f"\n## {p['id']} -- {p['title']}\n")
            parts.append(f"rated: campaign axis {p['campaign_axis']}, "
                         f"gate axis {p['gate_axis']}\n")
            if p.get("speaks_to_deferral"):
                parts.append(f"\n**Speaks to a question we set aside:** "
                             f"{p['speaks_to_deferral']}\n")
            if p.get("acts_on"):
                parts.append(f"\n**What a strategy would do differently:** "
                             f"{p['acts_on']}\n")
            if p.get("adaptation"):
                parts.append(f"\n**How it adapts to our data:** "
                             f"{p['adaptation']}\n")
            parts.append(f"\n{p['abstract']}\n")
            if p["body"]:
                parts.append(f"\n### full text (first {len(p['body']):,} "
                             f"chars, for checking a claim)\n{p['body']}\n")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# gates 1, 2, 4
# --------------------------------------------------------------------------- #
def review_findings(md: str, parent: str) -> tuple[list[str], str]:
    """The first agentic seat: is this question worth spending trials on?

    The six mechanical gates are string checks -- slug, heading, vocabulary,
    grounding, family, length -- and none of them can tell you a question is
    *bad*, only that it is malformed, leaky, unsupported, drifting or long.
    `nq-s04-03` passed every one of them with a question whose champion made
    four trades and scored `-inf`, so `n_nulls` was 0 and twenty trials bought
    nothing. A reader would have seen in a minute that a strategy obeying that
    question could barely act.
    """
    return _seat("hypothesis", lambda m: m.review(md, parent))


def family_findings_agentic(parent: str, md: str) -> tuple[list[str], str]:
    """The second agentic seat: is this revision still the same question?

    The mechanical `family_changed` below is two similarity floors and this
    file's own docstring calls it the weakest gate. It catches a revision that
    changed the subject outright; it cannot catch one that drifted in small
    steps, which is the shape the failure actually takes. Mechanical first
    because it is free, this second and only on a draft that cleared it.
    """
    return _seat("family", lambda m: m.family_review(parent, md))


def _seat(name: str, call) -> tuple[list[str], str]:
    """Run one agentic seat. Returns its repair lines and what it did.

    **The status carries the verdict, not just "ran".** An empty finding list
    means two different things -- the seat read it and did not object, or the
    seat never ran -- and a question nobody reviewed should not be
    indistinguishable, a week later, from one that passed.

    Never fatal on its own error, which is a deliberate asymmetry with
    `audit/committee`, where a seat that fails is recorded as a VETO. Here a
    failed call would kill an overnight session, so it fails open -- and the
    status says so in words, and that string reaches the commit trailer.
    """
    try:
        import review_hypothesis
        r = call(review_hypothesis)
    except Exception as exc:                                     # noqa: BLE001
        print(f"      the {name} seat did not run ({type(exc).__name__}); "
              f"treating that as no objection, which it is not", flush=True)
        return [], f"DID-NOT-RUN ({type(exc).__name__}) -- NOT reviewed"
    status = f"{r['verdict']} ({r['model_served']})"
    if r.get("unreadable"):
        status += " -- unreadable verdict, NOT reviewed"
    if r["verdict"] != "OBJECT":
        return [], status
    return [f"{name} reviewer: {c}" for c in r["concerns"]], status


def lint_findings(md: str) -> list[str]:
    from src.core.loop.hypothesis import findings
    return findings(md)


def grounding_findings(md: str, sources: list[str]) -> list[str]:
    import grounding
    return grounding.findings(md, *sources)


def family_changed(parent: str, child: str) -> str:
    """The mechanical family gate, and the weakest of the six -- which is
    exactly why `family_findings_agentic` now sits behind it.

    A title that no longer overlaps the parent's, or a body sharing almost
    nothing with it, is a new question wearing an old name. Measured as a
    similarity floor rather than asserted, so the number is arguable in the open.
    """
    ph = parent.lstrip().splitlines()[0].lstrip("# ").lower()
    ch = child.lstrip().splitlines()[0].lstrip("# ").lower()
    stop = {"does", "do", "a", "an", "the", "on", "in", "of", "and", "or", "it",
            "is", "are", "for", "to", "that", "what", "when", "give", "back"}
    pw = {w for w in re.findall(r"[a-z]+", ph) if w not in stop and len(w) > 2}
    cw = {w for w in re.findall(r"[a-z]+", ch) if w not in stop and len(w) > 2}
    overlap = len(pw & cw) / max(1, len(pw))
    body = difflib.SequenceMatcher(None, parent, child).ratio()
    # **`or`, not `and`. Measured 2026-10-04, and the `and` made this gate very
    # nearly inert.** A revision is written by handing a model its parent and
    # asking for a revision, so it reuses the parent's standing sections and
    # body similarity is always high -- 65-77% across the five real machine
    # refinements in this repo's history. Requiring BOTH terms to be low meant
    # the body term alone vetoed the gate no matter how far the title moved. A
    # planted revision that renamed the question from "does an outsized move
    # continue" to "do particular clock hours carry a directional bias" scored
    # 20% title overlap and 81% body similarity, and passed.
    #
    # Checked against ground truth before changing: all five real refinements
    # score 100% title overlap, so `or` fires on none of them. The body term
    # keeps a thin margin -- the tightest real refinement is 32% against a 30%
    # floor -- so it may occasionally ask for a repair it did not need. That is
    # cheap: a repair request, not a refusal.
    #
    # It still cannot catch a drift that keeps the title. That one needs
    # `family_findings_agentic`, which is the point of having both.
    if overlap < 0.34 or body < 0.30:
        why = ("its heading shares only" if overlap < 0.34 else
               "its heading shares")
        return (f"the revision looks like a different question: {why} "
                f"{overlap:.0%} of the parent's content words and the "
                f"bodies are {body:.0%} similar.\n    parent: {ph}\n    child:  "
                f"{ch}")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaign", required=True,
                    help="the campaign whose outcome informs the revision")
    ap.add_argument("--parent", type=pathlib.Path, default=None,
                    help="the hypothesis to refine; defaults to the campaign's")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--commit", action="store_true",
                    help="commit it (Amendment 11). Without this, it writes the "
                         "file and stops.")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not prereg.in_force(11) and a.commit:
        sys.exit("Amendment 11 is not in force, so a machine may not commit a "
                 "hypothesis. Drop --commit, or sign the amendment.")

    d = CAMPAIGNS / a.campaign
    if not (d / "tier2-summary.json").exists():
        sys.exit(f"{a.campaign} has no tier2-summary.json: it has not been "
                 f"reported yet, so there is nothing to refine against.")

    # ---- gate 5, before anything is read -----------------------------------
    leak = validation_readable(a.campaign)
    if leak:
        sys.exit(
            "REFUSED. A validation artefact is readable for "
            f"{a.campaign}:\n  " + "\n  ".join(leak) + "\n\n"
            "Section 4: campaign k+1 may depend on prior campaigns' train "
            "results, never on their validation results. The honest reading is "
            "that the information must not be in the room, so this refuses "
            "rather than promising not to look. Refine during phase 1, before "
            "validation runs at the stop.")

    record = json.loads((d / "campaign.json").read_text()).get("record", {})
    parent_path = a.parent or (ROOT / record["hypothesis_path"])
    parent = parent_path.read_text()

    ev = evidence(a.campaign)
    papers = papers_for(parent)
    user = context(parent, ev, papers)
    print(f"parent      {parent_path.relative_to(ROOT)}")
    print(f"evidence    {len(ev['auditors'])} auditor(s), "
          f"{ev['what_the_strategy_did'].get('trades')} trades, "
          f"{len(papers)} paper(s)")
    print(f"withheld    {', '.join(ev['redacted'])}")
    print(f"context     {len(user)} chars")
    if a.dry_run:
        print("\ndry run: nothing was asked of a model and nothing written.")
        return 0

    # The committee's client, as stages 1-3 of retrieval use it and for the same
    # reason distil_paper.py gives: a fourth copy of the same HTTP call is a
    # fourth place for a default to drift. It also preflights, so a substituted
    # model is caught before a question is written under the wrong name.
    from audit.committee.llm import Client, preflight
    served = preflight(a.model)
    print(f"model       asked {a.model!r}, served {served!r}")
    client = Client(model=a.model, temperature=0.7,
                    max_tokens=REFINE_MAX_TOKENS)
    sources = [parent, user] + [p["body"] + p["abstract"] for p in papers]

    def _ask(extra: str = "") -> dict:
        """One call, retried only for a reply that was not usable JSON.

        The client repairs raw control characters inside strings, which is the
        failure that ended session-03 after three campaigns. This is the second
        line: a malformed reply gets a bounded number of further tries with the
        requirement restated, because a format fault should cost a few seconds
        rather than a five-campaign session.
        """
        from audit.committee.llm import LLMUnavailable
        import requests
        # A network fault is the same kind of thing as a malformed reply from
        # here: the endpoint did not give us a usable answer. Catching only
        # LLMUnavailable let a ReadTimeout escape and kill a standalone run --
        # the session driver retries the whole step, so this only showed up
        # outside one.
        retryable = (LLMUnavailable, requests.exceptions.Timeout,
                     requests.exceptions.ConnectionError)
        last = None
        for i in range(MAX_FORMAT_RETRIES + 1):
            try:
                return client.ask(PROMPT.read_text(), user + extra + (
                    "" if i == 0 else
                    "\n\n# Your last reply was not usable JSON\n"
                    "Return ONLY a JSON object with keys \"slug\" and "
                    "\"markdown\". Inside the markdown string every line break "
                    "must be written \\n and every tab \\t -- a real line "
                    "break inside a JSON string is invalid.\n"))
            except retryable as exc:
                last = exc
                print(f"      no usable reply "
                      f"(attempt {i + 1}/{MAX_FORMAT_RETRIES + 1}): "
                      f"{str(exc)[:110]}", flush=True)
        raise last

    reviews = 0
    _unreached = "NOT-REACHED (the mechanical gates were never all clean)"
    reviewer = family_seat = _unreached
    out = _ask()
    for attempt in range(MAX_LINT_REPAIRS):
        # Escalate to a different model rather than asking the same one again.
        if attempt == SWITCH_MODEL_AFTER and a.model != FALLBACK_MODEL:
            print(f"      {a.model} has missed a gate {attempt} times; asking "
                  f"{FALLBACK_MODEL} instead", flush=True)
            served = preflight(FALLBACK_MODEL)
            client = Client(model=FALLBACK_MODEL, temperature=0.7,
                            max_tokens=REFINE_MAX_TOKENS)
        md = out.get("markdown") or ""
        slug = str(out.get("slug") or "").strip().lower()
        leaks = lint_findings(md) if md else ["empty"]
        loose = grounding_findings(md, sources) if md else []
        fam = family_changed(parent, md) if md else ""
        words = len(md.split())
        length = ("" if MIN_WORDS <= words <= MAX_WORDS else
                  f"{words} words, outside {MIN_WORDS}-{MAX_WORDS}")
        # **Shape is repairable like everything else.** The slug and the
        # opening heading were checked only after this loop, so a model could
        # satisfy lint, grounding, family and length for all eight rounds and
        # still end the run with nothing written, over a seven-word slug
        # nobody had told it was wrong. That is what happened on the first
        # end-to-end run against nq-t72b-01. A gate the proposer is never
        # shown is not a gate, it is a trap.
        shape = []
        if not SLUG.match(slug):
            shape.append(f"slug is not 2-6 kebab-case words: {slug!r}")
        if md and not md.lstrip().startswith("#"):
            shape.append("markdown does not start with a '#' heading")
        if not (leaks or loose or fam or length or shape):
            # **The reviewer runs last, and only on a mechanically clean file.**
            # It is a model call, so asking it about a draft that still has a
            # lint leak spends a minute to be told something we already knew.
            # Bounded separately from the mechanical repairs because its
            # objections are judgements, not defects: two rounds of "a reviewer
            # disagrees" is argument, and the gate is not a debating society.
            if reviews < MAX_REVIEW_REPAIRS:
                hyp_rev, reviewer = review_findings(md, parent)
                fam_rev, family_seat = family_findings_agentic(parent, md)
                rev = hyp_rev + fam_rev
                if rev:
                    reviews += 1
                    print(f"      reviewer objected ({reviews}/"
                          f"{MAX_REVIEW_REPAIRS}): {rev[0][:90]}", flush=True)
                    out = _ask("\n\n# A reviewer read your revision and "
                               "objected\n\nIt was shown the question and its "
                               "parent, and no score of any kind. Answer the "
                               "objection by changing the question, or if you "
                               "think it is wrong, say so inside the revision "
                               "in one sentence and leave the question as it "
                               "is.\n\n"
                               + "".join(f"- {c}\n" for c in rev))
                    continue
            elif reviews:
                capped = (f" -- objected {reviews}x, cap reached, the last "
                          f"revision was taken WITHOUT a seat clearing it")
                reviewer += capped
                family_seat += capped
            break
        note = ["\n\n# Your previous attempt was refused. Fix ALL of it at once.\n"]
        if shape:
            note.append("\n## The shape of the reply is wrong\n")
            note += [f"- {x}\n" for x in shape]
            note.append(
                "- The slug is the JSON \"slug\" field, not a heading. Two to "
                "six lowercase words joined by single hyphens, for example "
                "`opening-range-breakout`. **Name** the question, do not "
                "describe it -- if your slug reads like a sentence it is too "
                "long.\n")
        if leaks:
            note.append("\n## It tells the strategy agent how it is judged\n")
            note += [f"- {x}\n" for x in leaks[:10]]
        if loose:
            note.append("\n## Numbers or quotes that are in no source\n")
            note += [f"- {x}\n" for x in loose[:10]]
        if fam:
            note.append(f"\n## It is no longer the same question\n- {fam}\n")
        if length:
            over = words - MAX_WORDS
            # Name the sections and their sizes. "You are 207 words over, and
            # these are your three longest sections" is actionable; "too long"
            # is not, and five rounds of "too long" is what ended session-04.
            import re as _re
            secs = [(h.strip(), len(b.split())) for h, b in
                    _re.findall(r"^(#{2,3} .*)$([^#]*)", md, _re.M)]
            secs.sort(key=lambda x: -x[1])
            note.append(
                f"\n## Length -- a hard gate, and you have now missed it "
                f"{attempt + 1} time(s)\n"
                f"- You wrote **{words} words**. The ceiling is **{MAX_WORDS}**. "
                f"You are **{over} words over** and must cut at least that "
                f"much.\n"
                f"- Your longest sections are: "
                + "; ".join(f"**{h}** ({n} words)" for h, n in secs[:4]) + ".\n"
                f"- **Cut whole sentences and whole bullets**, do not compress "
                f"every sentence into something unreadable. What must survive is "
                f"the question itself, what the evidence showed, and what the "
                f"revision therefore demands.\n"
                f"- The parent was {len(parent.split())} words. A revision does "
                f"not have to be longer than its parent, and if yours is, the "
                f"extra length has to be buying something.\n")
        out = _ask("".join(note))

    md = str(out.get("markdown") or "")
    slug = str(out.get("slug") or "").strip().lower()
    problems = []
    if not SLUG.match(slug):
        problems.append(f"slug is not 2-6 kebab-case words: {slug!r}")
    if not md.lstrip().startswith("#"):
        problems.append("markdown does not start with a '#' heading")
    problems += [f"lint: {x}" for x in lint_findings(md)]
    problems += [f"grounding: {x}" for x in grounding_findings(md, sources)]
    fam = family_changed(parent, md)
    if fam:
        problems.append(f"family: {fam}")
    words = len(md.split())
    if not (MIN_WORDS <= words <= MAX_WORDS):
        problems.append(f"{words} words, outside {MIN_WORDS}-{MAX_WORDS}")
    if problems:
        print("\nNOT WRITTEN. Amendment 11's gates:")
        for p in problems[:14]:
            print(f"  - {p}")
        return 1

    dest = HYPOTHESES / f"{slug}.md"
    if dest.exists():
        dest = HYPOTHESES / f"{slug}-{dt.datetime.now():%m%d%H%M}.md"
    diff = "\n".join(difflib.unified_diff(
        parent.splitlines(), md.splitlines(),
        fromfile=str(parent_path.relative_to(ROOT)),
        tofile=str(dest.relative_to(ROOT)), lineterm=""))
    # The real `config/hypotheses/` always exists; a rehearsal's does not until
    # the first question is written into it.
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(md if md.endswith("\n") else md + "\n")
    print(f"\nwrote {dest.relative_to(ROOT)}  ({words} words)")
    print(f"  mechanical gates passed: shape, lint, grounding, family, "
          f"length, no validation readable")
    # The agentic seats are judgements, so they are reported one by one rather
    # than summed into the mechanical count. NO_OBJECTION is NOT approval --
    # neither seat can approve a question, only fail to object to it.
    for label, st in (("hypothesis seat", reviewer), ("family seat", family_seat)):
        print(f"  {label:16} {st}"
              + ("   (not approval)" if st.startswith("NO_OBJECTION") else ""))
    if "NOT reviewed" in reviewer or "NOT reviewed" in family_seat:
        print("  ** at least one seat never read this question. That is not "
              "the same as no objection. **")

    if a.commit and _os.environ.get("PRACTICUM_RUNS_ROOT"):
        print("\n  REHEARSAL: written but NOT committed. Amendment 13 -- a "
              "rehearsal does not\n  touch the real pre-registration, so the "
              "git commit is the one step it cannot test.")
        return 0
    if not a.commit:
        print("\nNOT committed. Read it, then commit it -- or pass --commit, "
              "which Amendment 11 permits.")
        return 0

    msg = (
        f"hypothesis: refine {parent_path.stem} after {a.campaign}\n\n"
        f"MACHINE-WRITTEN AND MACHINE-COMMITTED under Amendment 11. No human "
        f"read this before it was committed.\n\n"
        f"Parent: {parent_path.relative_to(ROOT)}\n"
        f"Informed by: {a.campaign} -- train results and four auditors' "
        f"objections only.\n"
        f"Withheld from the refiner: {'; '.join(ev['redacted'])}.\n"
        f"Model: {a.model} (served {served})\n"
        f"Papers cited: {', '.join(p['id'] for p in papers) or 'none'}\n\n"
        f"Gates passed: hypothesis lint, grounding, family-unchanged, length, "
        f"and no validation artefact was readable for {a.campaign} when this "
        f"ran.\n\n"
        f"Amendment 11's containment: this may spend trials and may NOT support "
        f"a claim. If a family containing it reaches L2 and passes, the pass is "
        f"held until a human has read every machine-written question in it.\n\n"
        f"--- diff against the parent ---\n{diff[:4000]}\n\n"
        f"Machine-Written: {a.campaign}\n"
        # **The record that was missing.** Until now the only trace of what the
        # agentic seats said was stdout, so a question nobody reviewed looked,
        # a week later, exactly like one that passed. `git log` now answers it.
        f"Hypothesis-Review: {reviewer}\n"
        f"Family-Review: {family_seat}\n")
    subprocess.run(["git", "add", str(dest)], cwd=str(ROOT), check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=str(ROOT), check=True)
    print(f"  committed, marked `Machine-Written: {a.campaign}`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
