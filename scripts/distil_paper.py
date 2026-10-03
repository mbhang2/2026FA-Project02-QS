#!/usr/bin/env python3
"""Stage 3 of the retrieval pipeline: one paper becomes one registerable question.

    python scripts/distil_paper.py --paper 2605.04004v3 --dry-run
    python scripts/distil_paper.py --paper 2605.04004v3

Design: `docs/design/paper-retrieval.md` stage 3. Prompt:
`config/prompts/paper-distil.md`. Governed by Amendment 8 of
`docs/design/pre-registration.md`, which is what makes a machine-written
hypothesis a valid pre-registration.

**This is the only stage that writes something a campaign can be registered on.**
Stages 0-2 fill in a registry; nothing downstream of them had ever produced a
file, so no paper in 224 had ever reached a campaign. This closes that.

## Three things this script refuses to do

**1. It will not write a file that mentions the gate.** The output is linted with
`src.core.loop.hypothesis.lint` -- the same function `nq_campaign.py` runs at
registration -- **before** anything is written, and a failure is a refusal with
every finding named. A machine writing prose that goes verbatim into the
strategy agent's prompt is the sharpest version of the leak the lint exists for:
a human who writes "check it holds out of sample" is one careless sentence, a
model that learns the phrasing writes it every time.

**2. It will not write while a campaign is in flight.** `config/hypotheses/*.md`
is in `ARTIFACT_GLOBS`, so **adding a file to that directory changes the artifact
fingerprint of every subsequent run** -- and the fingerprint is compared at load
and again after the run, so a file appearing mid-campaign invalidates the trial
that straddles it. A stray file in that directory already did this once, on
`nq-orderflow-02`. The guard is a process check, not a note.

**3. It will not touch an existing hypothesis file.** A hypothesis's commit date
is its pre-registration date. Overwriting one would re-date a question after its
campaign had run, so a colliding slug is an error and the human picks another.

## What it costs in N: nothing, and why that is not obvious

Section 4 of the design: retrieval, screening, ranking and distillation never
look at our market data, so none of them is a look. **The moment a candidate
hypothesis is backtested to decide whether to keep it, it is a look and it
counts.** This script therefore produces a file and stops. It does not score it,
rank it against another distillation, or run anything -- and a pipeline that
generated ten hypotheses and registered the most promising would have taken ten
campaigns' worth of looks while reporting one.

Build-time, like the other three stages: nothing under `src/core/` may import it.
It imports `src.core.loop.hypothesis` in the other direction, which is the
allowed one -- a build-time script may read the run-time contract it must satisfy.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REGISTRY = ROOT / "data" / "papers" / "registry.json"
RAW = ROOT / "data" / "papers" / "raw"
PROMPT = ROOT / "config" / "prompts" / "paper-distil.md"
HYPOTHESES = ROOT / "config" / "hypotheses"
# **gemini-2.5-pro, not claude-haiku-4-5.** Measured 2026-10-03 against the
# gateway: every Claude model except haiku is 403 FORBIDDEN, and haiku itself is
# substituted to gemini-3.1-pro-preview above ~157k characters. So haiku was the
# weakest authorised model, doing a judgement task.
#
# gemini-2.5-pro is chosen over deepseek-v4-pro deliberately: deepseek writes the
# strategies, and the model that judges a paper should not be the model that
# writes the code. Different family, authorised, 39s on a 167k prompt -- and the
# prompts here are a fraction of that.
MODEL = "gemini-2.5-pro"

# The budget from the design, in words rather than tokens because a word count is
# what a human can check. The design's figure is **800-1,500 tokens**
# (`docs/design/paper-retrieval.md`), which at ~1.33 tokens a word is roughly
# **600-1,125 words**.
#
# **This ceiling was 900, and that was my error rather than the design's.** I set
# it tighter than the document it implements, and it then refused a file the
# design would have accepted. Corrected to the design's own number.
#
# Recorded because the behaviour that surfaced it is worth keeping: **with the
# paper body in context, `claude-haiku-4-5` writes ~950 words and does not
# shorten on request.** Asked three times, it returned 929 then 956 -- longer
# each time. This is not a threshold moved until something passes, which
# CLAUDE.md forbids by name: the ceiling is the design's, and the model's refusal
# to compress is a fact about the model that belongs in the record.
# How much of the paper body reaches the prompt. The longest extracted paper is
# 131,002 characters, roughly 33,000 tokens, which is affordable once per
# campaign and not per trial -- this prompt is paid once, the campaign prompt is
# paid per trial. Capped anyway so one enormous paper cannot dominate.
FULLTEXT_CAP = 120_000

MIN_WORDS = 350
MAX_WORDS = 1_125

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){1,5}$")


def _grounding_findings(md: str, paper_id: str) -> list[str]:
    """Quantities and quoted spans the sources do not contain.

    Bryan Rodriguez's `aider-validate` idea, deterministic core only -- see
    `scripts/grounding.py` for what it does and does not catch, and for the
    fabricated number in this script's first real distillation that motivated it.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "grounding", pathlib.Path(__file__).with_name("grounding.py"))
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    return g.findings(md, *g.sources_for(paper_id))


def _lint_findings(md: str) -> list[str]:
    """`hypothesis.findings`, imported here so the repair loop and the final
    check cannot drift apart. Same function the campaign runs at registration."""
    from src.core.loop.hypothesis import findings
    return findings(md)


# **32,768, not 8,192.** Sixth application of one lesson: the budget has to cover
# the model's reasoning, not just its answer. A distilled hypothesis is ~1,300
# tokens of output, and `gemini-3.1-pro-preview` was measured spending 12,004 to
# 26,752 tokens thinking before its first visible token. The other five sites are
# listed in `audit/committee/llm.py`.
DISTIL_MAX_TOKENS = 32_768


def _client(model: str = MODEL):
    """The committee's client, as stages 1 and 2 use it and for the same reason:
    a fourth copy of the same HTTP call is a fourth place for a default to drift."""
    from audit.committee.llm import Client, preflight
    served = preflight(model)
    return Client(model=model, temperature=0.0,
                  max_tokens=DISTIL_MAX_TOKENS), served


def campaign_in_flight() -> str:
    """The name of a running campaign, or "". A process check, deliberately.

    `config/hypotheses/*.md` is fingerprinted, so a new file there changes
    `artifact_fingerprint` for every run that starts afterwards and invalidates
    one that is mid-flight. Asking the OS is the only way to know; a lock file
    would be a second thing to keep in sync.
    """
    out = subprocess.run(["pgrep", "-fl", "nq_campaign.py --stage run"],
                         capture_output=True, text=True).stdout.strip()
    if not out:
        return ""
    m = re.search(r"--name\s+(\S+)", out)
    return m.group(1) if m else "a campaign"


def context(paper: dict, row: dict) -> str:
    """What the distiller is given: the paper, and stage 1's adaptation.

    Stage 2's relevance is deliberately NOT included. It is a judgement about
    whether to spend a campaign, and a distiller told "this was rated highly"
    would write a more confident question than the evidence supports.
    """
    parts = [
        "### The paper", "",
        f"id: {row['id']}",
        f"Title: {paper.get('title', row.get('title', '(none)'))}", "",
        f"Abstract: {paper.get('abstract', '(none)')}", "",
        f"Its claim, as stage 1 recorded it: {row.get('claim') or '(not recorded)'}",
        "",
        f"Stage 1 judged our data **{row.get('feasibility')}** for it.",
    ]
    # **The body, when stage 0b has fetched it.** An abstract is ~1,300
    # characters and states no rule, no threshold and no friction model; the
    # body is 20-245x that. Distilling from the abstract produced a hypothesis
    # whose premise was a number the paper does not contain (see
    # `scripts/grounding.py`), which is the predictable result of asking for
    # specifics from a text that has none.
    full = ROOT / "data" / "papers" / "text" / f"{row['id']}.txt"
    if full.exists():
        body = full.read_text()
        if len(body) > FULLTEXT_CAP:
            # Head and tail: the claims and the method are at the front, the
            # results and the numbers at the back, and what is cut is the
            # middle of the related work.
            half = FULLTEXT_CAP // 2
            body = (body[:half] + "\n\n[... middle of the paper omitted "
                    f"({len(body) - FULLTEXT_CAP:,} characters) ...]\n\n"
                    + body[-half:])
        parts += ["", "### The paper itself", "",
                  "This is the full text, extracted from "
                  f"{row.get('fulltext_source', 'the source')}. **Quote its "
                  "figures exactly as it gives them.**", "", body]
    if row.get("adaptation"):
        parts += ["", "### How stage 1 said it must be adapted to be testable here",
                  "", str(row["adaptation"])]
    if row.get("feasibility_reason"):
        parts += ["", "### Why stage 1 judged it that way", "",
                  str(row["feasibility_reason"])]
    return "\n".join(parts)


# **Bounded, and safe for the same reason the agent's static repair is.** The
# findings are properties of the TEXT the model just wrote -- which banned word
# appeared on which line -- and carry nothing about our market data, so iterating
# on them leaks no selection signal. Distillation costs no N (design §4), so a
# repair costs nothing but a call. It is bounded anyway: a model that cannot say
# it in three attempts is telling us the claim cannot be stated without the
# vocabulary, and that is worth hearing rather than working around.
#
# **3, not 2, since stage 0b.** Handing the model the whole paper makes the leak
# MORE likely, not less: a falsification study's body is 44,000 characters
# saturated with "out-of-sample", "validation" and "walk-forward", and the
# distiller has to write about what the paper found without reusing the words it
# found it in. Two repairs were not enough for `2605.04004v3`; the note now also
# shows the offending LINE, which is what makes the fix locatable.
MAX_LINT_REPAIRS = 3


def _too_long(md: str) -> str:
    """The length finding, or "". Repairable like the others: a file 29 words over
    is a file the model can shorten, and refusing it outright wastes the whole
    distillation. Handing the paper body to the model made this the binding check
    -- it writes more when it has more to write about."""
    n = len(md.split())
    if n > MAX_WORDS:
        return (f"{n} words, {n - MAX_WORDS} over the ceiling of {MAX_WORDS}")
    if md.strip() and n < MIN_WORDS:
        return (f"{n} words, {MIN_WORDS - n} under the floor of {MIN_WORDS}")
    return ""


def _repair_note(attempt: int, leaks: list[str], loose: list[str],
                 length: str = "", uncited: str = "") -> str:
    """What the model is told when its file is refused.

    **Every finding, not the most important one.** The first version reported one
    category at a time, on the reasoning that there is no point grounding a file
    that will be refused for leaking anyway. That was wrong: the model then fixed
    one constraint and broke another, and three rounds on `2605.04004v3` went
    leak, then length, then missing citation without ever converging. Four checks
    reported one at a time cannot be satisfied in three attempts; four reported
    together can be satisfied in one.

    All four are properties of the text the model just wrote, so none of them
    leaks anything about our market data -- the same argument that makes the
    agent's static repair safe.
    """
    parts = [f"\n\n### Your previous answer was refused, attempt "
             f"{attempt + 1} of {MAX_LINT_REPAIRS + 1}\n\n"
             "Fix **all** of the following in one rewrite. Fixing one and "
             "breaking another is how this loop fails.\n"]

    if leaks:
        words = sorted({f.split("'")[1] for f in leaks if "'" in f})
        detail = "\n".join("    " + x for x in leaks[:12])
        parts.append(f"""#### 1. Forbidden terms ({len(leaks)}): {', '.join(repr(w) for w in words)}

{detail}

Every one reaches the strategy agent and tells it how its work will be judged,
which is the one thing it may not know.

**The paper you were given is full of these words.** That is expected -- it is a
study about how to evaluate rules. You are not writing about how to evaluate
rules. You are writing about what is true of the market, and these words belong
to the part you leave out.""")

    if loose:
        detail = "\n".join("    " + x for x in loose[:12])
        parts.append(f"""#### 2. Quantities your sources do not contain ({len(loose)})

{detail}

**Every number you write must be one you can point at** -- in the paper's own
text, or in what this prompt told you about our data. A cited number that is not
in the source is worse than no number, because the citation makes the error look
sourced.

Quote the source's figure exactly as the source gives it, or drop the sentence.
Do not round, do not approximate, and do not infer a range from a single value.
If the paper says "0.07-1.50", write 0.07-1.50, not "about 0.5 to 1.5". **A range
is checked as a pair**, so half-right is wrong.""")

    if length:
        parts.append(f"""#### 3. Length: {length}

Aim for 450 to 700 words. This file is pasted into the prompt of every trial in
the campaign, so its length is paid for hundreds of times.

Cut the paper's framing, not the claim. What must stay: what is true of the
market, what the strategy agent may choose, what it may not, and what the answer
would teach either way. What can go: restating the paper's motivation, listing
its related work, and any sentence that explains rather than specifies.""")

    if uncited:
        parts.append(f"""#### 4. The paper id `{uncited}` does not appear

Without it a result cannot be traced back to the paper that proposed it, and the
whole corpus would have to be frozen instead of referenced. Put it in the opening
sentences, exactly as `{uncited}`.""")

    return "\n\n".join(parts)


def distil_one(client, paper: dict, row: dict) -> dict:
    """One call, one hypothesis, with a bounded repair on a lint failure."""
    user = context(paper, row)
    out = client.ask(PROMPT.read_text(), user)
    for attempt in range(MAX_LINT_REPAIRS):
        md_so_far = out.get("markdown") or ""
        leaks = _lint_findings(md_so_far)
        loose = _grounding_findings(md_so_far, row["id"]) if md_so_far else []
        length = _too_long(md_so_far)
        uncited = "" if row["id"] in md_so_far else row["id"]
        if not (leaks or loose or length or uncited):
            break
        out = client.ask(PROMPT.read_text(), user + _repair_note(
            attempt, leaks, loose, length, uncited))

    slug = str(out.get("slug") or "").strip().lower()
    if not SLUG.match(slug):
        raise ValueError(f"slug is not 2-6 kebab-case words: {slug!r}")

    md = str(out.get("markdown") or "")
    if not md.lstrip().startswith("#"):
        raise ValueError("markdown does not start with a '#' heading")

    words = len(md.split())
    if not (MIN_WORDS <= words <= MAX_WORDS):
        raise ValueError(
            f"{words} words, outside {MIN_WORDS}-{MAX_WORDS}. This file is pasted "
            "into every prompt of the campaign, so its length is paid for once per "
            "trial; too short and it is not a question.")

    # **The lint, before anything is written.** Same function the campaign runs at
    # registration, so a file that passes here cannot fail there.
    leaks = _lint_findings(md)
    if leaks:
        raise ValueError(
            "the distilled hypothesis tells the strategy agent about the gate:\n    "
            + "\n    ".join(leaks[:12])
            + f"\n  ({len(leaks)} finding(s)). Every byte of this file reaches the "
              "agent. Not written.")

    # **And the grounding check.** A pre-registered question whose premise is a
    # number the paper does not contain is worse than no question: the citation
    # makes the error look sourced. The first distillation of 2605.04004v3 wrote
    # "0.5-1.5" where the paper says "0.07-1.50".
    loose = _grounding_findings(md, row["id"])
    if loose:
        raise ValueError(
            "the distilled hypothesis states quantities its sources do not:\n    "
            + "\n    ".join(loose[:12])
            + f"\n  ({len(loose)} finding(s)). A cited number that is not in the "
              "source makes an error look sourced. Not written.")

    # The chain back to the source. Without it a result cannot be traced to the
    # paper that proposed it, and the corpus would have to be frozen instead.
    if row["id"] not in md:
        raise ValueError(
            f"the file does not cite the paper id {row['id']}, so a result could "
            "not be traced back to its source")

    return {"slug": slug,
            "title": str(out.get("title") or "").strip(),
            "markdown": md if md.endswith("\n") else md + "\n",
            "adapted_claim": out.get("adapted_claim"),
            "cannot_test": out.get("cannot_test"),
            "sets_aside": out.get("sets_aside") or [],
            "words": words}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paper", required=True, help="a registry id, e.g. 2605.04004v3")
    ap.add_argument("--model", default=MODEL,
                    help=f"which model writes the question. Default {MODEL}. "
                         "Recorded on the registry row -- a machine-written "
                         "pre-registration has to say which machine "
                         "(Amendment 8). Measured: claude-haiku-4-5 cannot keep "
                         "a falsification paper's own vocabulary out of the file "
                         "once it is given the body.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the file and the checks; write nothing")
    a = ap.parse_args()

    if not REGISTRY.exists():
        sys.exit(f"no registry at {REGISTRY}; run the earlier stages first")
    reg = json.loads(REGISTRY.read_text())
    row = reg["papers"].get(a.paper)
    if row is None:
        sys.exit(f"{a.paper} is not in the registry")
    if row.get("feasibility") not in ("direct", "adaptable"):
        sys.exit(f"{a.paper} was screened {row.get('feasibility')!r}; stage 1 said "
                 "its claim cannot be tested on our data")
    if row.get("hypothesis_path"):
        sys.exit(f"{a.paper} already has {row['hypothesis_path']}. A hypothesis's "
                 "commit date is its pre-registration date, so it is not rewritten.")

    running = campaign_in_flight()
    if running and not a.dry_run:
        sys.exit(
            f"{running} is running. config/hypotheses/*.md is in ARTIFACT_GLOBS, so "
            "adding a file there changes artifact_fingerprint and invalidates the "
            "trial that straddles it -- this happened to nq-orderflow-02. Wait for "
            "the campaign, or use --dry-run.")

    f = RAW / f"{a.paper}.json"
    paper = json.loads(f.read_text()) if f.exists() else row

    client, served = _client(a.model)
    print(f"distilling {a.paper}  model {a.model} (served {served})")
    got = distil_one(client, paper, row)

    dest = HYPOTHESES / f"{got['slug']}.md"
    if dest.exists():
        sys.exit(f"{dest} already exists. A hypothesis is written once; pick "
                 f"another slug or delete nothing.")

    print(f"  slug        {got['slug']}")
    print(f"  words       {got['words']}  (budget {MIN_WORDS}-{MAX_WORDS})")
    print(f"  lint        clean")
    print(f"  cites id    yes")
    print(f"  adapted     {str(got['adapted_claim'])[:100]}")
    if got["cannot_test"]:
        print(f"  cannot test {str(got['cannot_test'])[:100]}")
    for x in got["sets_aside"][:4]:
        print(f"  sets aside  {str(x)[:92]}")

    if a.dry_run:
        print("\n" + "-" * 78)
        print(got["markdown"])
        print("-" * 78)
        print("dry run: nothing written")
        return 0

    dest.write_text(got["markdown"])
    row["hypothesis_path"] = str(dest.relative_to(ROOT))
    row["distilled"] = {"slug": got["slug"], "words": got["words"],
                        "adapted_claim": got["adapted_claim"],
                        "cannot_test": got["cannot_test"],
                        "sets_aside": got["sets_aside"],
                        "model": a.model, "model_served": served}
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    print(f"\n  wrote {dest.relative_to(ROOT)}")
    print(f"  wrote {REGISTRY.relative_to(ROOT)}  ({a.paper}.hypothesis_path)")
    print("\nNOT registered. Read the file, commit it, and only then:")
    print(f"  python scripts/nq_campaign.py --stage register --name <id> \\")
    print(f"         --hypothesis {dest.relative_to(ROOT)} ...")
    print("The commit is the pre-registration. A human reads it first "
          "(Amendment 8).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
