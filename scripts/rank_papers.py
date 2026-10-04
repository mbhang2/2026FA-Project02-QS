#!/usr/bin/env python3
"""Stage 2 of the retrieval pipeline: is this paper worth our next campaign?

    python scripts/rank_papers.py --dry-run
    python scripts/rank_papers.py

Design: `docs/design/paper-retrieval.md` stage 2. Prompt:
`config/prompts/paper-rank.md`.

Stage 1 decided a paper's claim *could* be tested here. This decides whether it is
*worth* testing, and what it would change about the question asked next -- which is
the thing an L2 loop actually consumes.

**The "what we know" block is assembled from committed artifacts, not maintained by
hand.** An earlier draft of the design specified ranking against a list of open
questions and noted that no such list existed. It does: every hypothesis file
carries a *What this is not asking for* section naming what that campaign set
aside, written before the campaign ran. All four of ours defer regime models with
"that is a later and harder question". Those deferrals are the open questions, and
they are already in the repository with dates attached.

Three sources, three purposes:

  config/hypotheses/*.md          what was asked, and what was deliberately not
  runs/campaigns/*/campaign.json  which questions are now closed
  docs/datasets/nq-es-qqq.md      what is falsified and must not be re-explored

**Outcomes are supplied as closure, never as a target.** The prompt says so
explicitly. Ranking papers by resemblance to what scored well would be selection on
data already spent -- retrieval and ranking cost no N precisely because they do not
look at the market, and steering them by past scores would quietly spend it.

Build-time, like the other scripts: nothing under `src/core/` may import it.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REGISTRY = ROOT / "data" / "papers" / "registry.json"
RAW = ROOT / "data" / "papers" / "raw"
PROMPT = ROOT / "config" / "prompts" / "paper-rank.md"


def _prompt_digest() -> str:
    import hashlib
    return hashlib.sha256(PROMPT.read_bytes()).hexdigest()[:16]
HYPOTHESES = ROOT / "config" / "hypotheses"
CAMPAIGNS = ROOT / "runs" / "campaigns"
DATASET_DOC = ROOT / "docs" / "datasets" / "nq-es-qqq.md"

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
LEVELS = ("high", "medium", "low")
# What a `gate_relevance` paper bears on. Closed set, so a row can be grouped
# rather than read: the gate itself, how trials are counted, the falsified-families
# list, or the behaviour of the loop.
BEARS_ON = ("gate", "accounting", "falsified_family", "loop")


def deferrals() -> list[str]:
    """The `What this is not asking for` bullets, per hypothesis. The open
    questions, written down before each campaign rather than reconstructed after."""
    out = []
    for f in sorted(HYPOTHESES.glob("*.md")):
        body = f.read_text()
        m = re.search(r"## What this is not asking for\n(.*?)(?=\n## |\Z)", body, re.S)
        if not m:
            continue
        bullets = [re.sub(r"\s+", " ", b).strip()
                   for b in re.findall(r"^- (.+?)(?=\n- |\n\n|\Z)", m.group(1), re.S | re.M)]
        if bullets:
            out.append(f"{f.stem}: " + " | ".join(bullets))
    return out


def closed_questions() -> list[str]:
    """What each campaign asked, and that it has been answered. The *question* and
    the *verdict*, deliberately without the scores -- see the module docstring."""
    out = []
    for d in sorted(CAMPAIGNS.iterdir()):
        rec = d / "campaign.json"
        summ = d / "tier2-summary.json"
        if not rec.exists():
            continue
        r = json.loads(rec.read_text()).get("record", {})
        hyp = r.get("hypothesis_path")
        if not hyp:
            continue
        verdict = "no summary yet"
        if summ.exists():
            g = json.loads(summ.read_text()).get("gate") or {}
            verdict = "ACCEPTED" if g.get("accept") else "rejected"
        out.append(f"{d.name}: asked {pathlib.Path(hyp).stem}, {verdict}")
    return out


def falsified() -> list[str]:
    if not DATASET_DOC.exists():
        return []
    m = re.search(r"## Falsified hypothesis families.*?\n(.*?)(?=\n## |\Z)",
                  DATASET_DOC.read_text(), re.S)
    if not m:
        return []
    return [re.sub(r"\s+", " ", b).strip()
            for b in re.findall(r"^- (.+?)(?=\n- |\n\n|\Z)", m.group(1), re.S | re.M)]


def asking_now(path: pathlib.Path) -> str:
    """The question we are working on, as its own block.

    **The gap this closes.** Nothing told the ranker what we are currently
    researching. It inferred from what each *past* hypothesis set aside, so a
    paper arriving while we chase trend reversal was judged against an old
    campaign's leftovers. Michael's example: an order-flow absorption paper is
    feasible on NQ tick data and would be marked down for being a closed
    question, which is right about the past and silent about the present.

    Deliberately the hypothesis TEXT and nothing else. It is linted prose that a
    human committed, so it cannot carry a threshold or a score -- and it must not
    carry an outcome: §4 allows campaign k+1 to depend on prior campaigns' train
    results and never their validation results, and a "how did this question do"
    block is exactly the door that opens.
    """
    from src.core.loop import hypothesis as H
    h = H.load(path)              # linted, so no gate vocabulary reaches the model
    return "\n".join([
        "### The question we are asking NOW",
        "",
        "Rank against this as well as against the history below. A paper that "
        "speaks to this question is worth more than one that speaks to a topic we "
        "have never touched; a paper that would change it is worth more still.",
        "",
        f"From `{path.name}`:",
        "",
        h.text.strip(),
    ])


def context_block(current: pathlib.Path | None = None) -> str:
    parts: list[str] = []
    if current is not None:
        parts += [asking_now(current), ""]
    parts += ["### Questions already asked, and closed",
              *(f"- {x}" for x in closed_questions()),
              "", "### What each hypothesis said it was NOT asking\n#### Read these as exclusions, not as open questions. A paper that needs\n#### what one of them rules out is less relevant, not more -- see the\n#### prompt's criterion 2, which this list was previously mislabelled for.",
              *(f"- {x}" for x in deferrals()),
              "", "### Falsified on this data -- do not re-explore",
              *(f"- {x}" for x in falsified())]
    # **The third kind of context, and the only one a paper can actually help
    # with** (Task 71 section 14). Questions we asked and did not answer, quoted
    # from the auditors who raised them. Drawn only from campaigns with no
    # validation artefact -- the same refusal `refine_hypothesis.py` applies, one
    # rule stated once and used twice. Never fatal: a ranking run must not die
    # because a committee file is malformed.
    try:
        import open_questions
        oq = open_questions.block()
        if oq:
            parts += ["", oq.rstrip()]
    except Exception:                                            # noqa: BLE001
        pass
    return "\n".join(parts)


def _client():
    from audit.committee.llm import Client, preflight
    # **16,384, not 1,024. Seventh site of one defect, 2026-10-04.**
    # `audit/committee/llm.py` already counted five and named this file as a
    # caller; the budget here was never raised. 91 of 102 papers failed with
    # `finish_reason='length', completion_tokens=1020` -- the model spent the
    # budget reasoning and the JSON was cut mid-string. It surfaced as "reply was
    # not JSON", which is why five previous instances each looked like something
    # other than a budget. A relevance verdict is one field and a sentence; the
    # budget is for the reasoning in front of it.
    return Client(model=MODEL, temperature=0.0, max_tokens=16_384), preflight(MODEL)


def rank_one(client, paper: dict, row: dict, ctx: str) -> dict:
    user = (f"{ctx}\n\n### The paper\n\n"
            f"Title: {paper['title']}\n\nAbstract: {paper.get('abstract','(none)')}\n\n"
            f"Stage 1 judged it **{row['feasibility']}**"
            + (f", adapting it thus: {row['adaptation']}" if row.get("adaptation") else "")
            + f".\nIts claim: {row.get('claim') or '(not recorded)'}")
    out = client.ask(PROMPT.read_text(), user)
    lvl = str(out.get("relevance", "")).strip().lower()
    if lvl not in LEVELS:
        raise ValueError(f"relevance not one of {LEVELS}: {out.get('relevance')!r}")
    if lvl == "high" and not out.get("hypothesis_edit"):
        # A paper worth fifty looks at the data has to change the question. If it
        # would leave the next hypothesis identical, it is not high.
        raise ValueError("high relevance with no hypothesis_edit")
    # **Axis 2, and it is not optional.** The first 224 papers were scored on axis 1
    # alone and it rated NONE of them high -- correctly, by its own rule, because the
    # most useful papers in the corpus are falsification studies on our own index that
    # offer no edge to act on. A missing axis here would silently reproduce that.
    gate = str(out.get("gate_relevance", "")).strip().lower()
    if gate not in LEVELS:
        raise ValueError(
            f"gate_relevance not one of {LEVELS}: {out.get('gate_relevance')!r}")
    if gate == "high" and not out.get("protocol_edit"):
        # Same bar as axis 1, for the same reason: high means it changes something.
        # A paper that would leave the gate, the accounting and the falsified list
        # exactly as they are is not high however well argued it is.
        raise ValueError("high gate_relevance with no protocol_edit")
    bears = out.get("bears_on")
    if bears is not None and str(bears) not in BEARS_ON:
        raise ValueError(f"bears_on not one of {BEARS_ON} or null: {bears!r}")
    return {"relevance": lvl,
            "relevance_reason": str(out.get("reason") or "").strip(),
            "acts_on": out.get("acts_on"),
            "hypothesis_edit": out.get("hypothesis_edit"),
            "speaks_to_deferral": out.get("speaks_to_deferral"),
            "gate_relevance": gate,
            "gate_reason": str(out.get("gate_reason") or "").strip(),
            "bears_on": bears,
            "protocol_edit": out.get("protocol_edit")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--asking", type=pathlib.Path, default=None,
                    help="the hypothesis file we are working on now, added to the "
                         "ranking context. Without it the ranker judges a paper "
                         "only against what PAST campaigns set aside, which is "
                         "silent about the present.")
    ap.add_argument("--rerank", action="store_true",
                    help="re-score papers that already have a relevance, to add "
                         "gate_relevance. The axis-1 label from the first pass is "
                         "preserved as relevance_first_pass when it changes, so "
                         "nothing is overwritten silently.")
    a = ap.parse_args()

    if not REGISTRY.exists():
        sys.exit(f"no registry at {REGISTRY}; run fetch_papers.py and screen_papers.py")
    reg = json.loads(REGISTRY.read_text())
    feasible = [r for r in reg["papers"].values()
                if r.get("feasibility") in ("direct", "adaptable")]
    if a.rerank:
        # Everything feasible that has not been scored on axis 2, OR was scored
        # under a different criterion.
        #
        # **A changed prompt is new evidence; the same prompt twice is not.** The
        # screen draws that line explicitly and the ranker did not: `--rerank`
        # only ever picked up papers missing axis 2, so after the axis-1
        # criterion was rewritten there was no way to re-rank the corpus under
        # it. Keyed on the digest rather than a flag, so it cannot be used to
        # re-roll a label under the criterion that produced it.
        here = _prompt_digest()
        todo = [r for r in feasible
                if r.get("gate_relevance") is None
                or r.get("rank_prompt_digest") != here][:a.limit]
        if not todo:
            print(f"every feasible paper is ranked under the current criterion "
                  f"({here}). Change config/prompts/paper-rank.md to re-rank; "
                  f"re-asking the same prompt is a re-roll."); return 0
        stale = [r["id"] for r in todo if r.get("gate_relevance") is not None]
        if stale:
            print(f"  {len(stale)} paper(s) were ranked under an older criterion "
                  f"and are being re-ranked under {here}\n")
    else:
        todo = [r for r in feasible if r.get("relevance") is None][:a.limit]
        if not todo:
            print("nothing feasible and unranked; --rerank adds the second axis "
                  "to papers scored before it existed"); return 0

    if a.asking is not None and not a.asking.exists():
        sys.exit(f"no hypothesis at {a.asking}")
    ctx = context_block(a.asking)
    client, served = _client()
    print(f"ranking {len(todo)} feasible papers  model {MODEL} (served {served})")
    print(f"  context: {len(closed_questions())} closed questions, "
          f"{len(deferrals())} deferral sets, {len(falsified())} falsified families"
          + (f", asking {a.asking.name}" if a.asking else ", NO current question")
          + "\n")

    def work(row):
        f = RAW / f"{row['id']}.json"
        paper = json.loads(f.read_text()) if f.exists() else row
        try:
            return row["id"], rank_one(client, paper, row, ctx), None
        except Exception as exc:                                  # noqa: BLE001
            return row["id"], None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(work, todo))

    counts: dict[str, int] = {}
    gcounts: dict[str, int] = {}
    for pid, got, err in results:
        if err:
            print(f"  {pid}  ERROR {err}"); continue
        counts[got["relevance"]] = counts.get(got["relevance"], 0) + 1
        gcounts[got["gate_relevance"]] = gcounts.get(got["gate_relevance"], 0) + 1
        print(f"  {pid}  campaign={got['relevance']:<7} gate={got['gate_relevance']:<7}"
              f" {(got['gate_reason'] or got['relevance_reason'])[:70]}")
        if not a.dry_run:
            row = reg["papers"][pid]
            was = row.get("relevance")
            if was is not None and was != got["relevance"]:
                # The first pass is part of the record. A re-rank that moved the
                # axis-1 label says something about the scale's stability, and
                # overwriting it would erase that.
                row["relevance_first_pass"] = was
            row.update(got)
            # **Which criterion produced this label.** A ranking is only
            # interpretable against the prompt that made it, and on 2026-10-01
            # the axis-1 criterion changed in a way that moved labels: it used to
            # rate a paper by whether its METHOD was runnable here, which rated
            # the whole corpus low. Recording the digest makes "ranked under the
            # old criterion" a queryable state instead of a thing to remember.
            row["rank_prompt_digest"] = _prompt_digest()
    print(f"\n  campaign axis {counts}")
    print(f"  gate axis     {gcounts}")
    moved = [p for p, g, e in results if not e and g
             and reg["papers"][p].get("relevance_first_pass")]
    if moved and not a.dry_run:
        print(f"  axis-1 label moved on re-rank for {len(moved)}: {', '.join(moved[:6])}")
    if a.dry_run:
        print("  dry run: registry not written"); return 0
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    print(f"  wrote {REGISTRY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
