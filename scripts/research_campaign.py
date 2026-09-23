#!/usr/bin/env python3
"""The research stage: papers in, a pre-registered NQ campaign out.

    python scripts/research_campaign.py --stage retrieve --name <name>   \
        [--query "all:..."] [--max-results K] [--fulltext-papers M]
    python scripts/research_campaign.py --stage draft --name <name>
    python scripts/research_campaign.py --stage register --name <name>   \
        --campaign-id research-<name>-01
    python scripts/research_campaign.py --stage run     --campaign-id <id>
    python scripts/research_campaign.py --stage nulls   --campaign-id <id>
    python scripts/research_campaign.py --stage report  --campaign-id <id>

The stages in order are the paper analogue of a campaign's own lifecycle, and
each one's output is what the next one is frozen against:

  1. `retrieve`  -- the human's brief (config/research/<name>.md) is searched
      against arXiv; the query and everything it returned are written to
      config/retrievals/<name>.json. Written once: the set is frozen the moment
      it exists, exactly as a campaign record is.
  2. `draft`     -- the research agent reads the brief, the frozen set and the
      data document, and writes a hypothesis. The output is linted against the
      same gate vocabulary as any committed hypothesis in a bounded repair loop,
      because once it is registered this text IS what the proposing agent
      reads. The draft is human-visible; nobody pushes it to config/hypotheses/
      but the human who reviewed it.
  3. `register`  -- requires the brief, the retrieval and the hypothesis all
      committed with clean worktrees: their commit dates are the
      pre-registration. Writes the campaign record to
      runs/campaigns/<campaign-id>/ with the hypothesis digest, the retrieval
      digest and the research config hashed in, and refuses to overwrite.
  4. `run/nulls/report` -- handed to scripts/nq_campaign.py unchanged, which
      runs the NQ campaign from the record (and reads the hypothesis from it).
      Real NQ train split, sign-flip null over round turns,
      `config/run.yaml`'s campaign: block for budget, q and correction.

**The retrieved papers reach the research agent and only the research agent.**
llm.LLMAgent assembles the proposing prompt from the hypothesis -- linted --
and nothing in this script is on that path. A paper full of p-values is
information the papers legitimately carry; the hypothesis is what must stay
clean, and the lint in src/core/loop/hypothesis.py is what enforces it.

**Nothing here runs the gate.** No threshold, no p-value, no N. The correction
happens in `report`, in nq_campaign.py, after every trial is in the ledger.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

RESEARCH_DIR = ROOT / "config" / "research"
RETRIEVALS_DIR = ROOT / "config" / "retrievals"
HYPOTHESES_DIR = ROOT / "config" / "hypotheses"
RUNS = ROOT / "runs" / "campaigns"
RESEARCH_RUNS = ROOT / "runs" / "research"

MODEL = "claude-haiku-4-5"      # the proposing LLMAgent, as in nq_campaign.py
RESEARCH_MODEL = "claude-haiku-4-5"
RESEARCH_TEMPERATURE = 0.3
N_SESSIONS = 384

NQ_PROFILE = "nq-train"


def cfg() -> dict:
    return yaml.safe_load((ROOT / "config" / "run.yaml").read_text())


def research_run_facts() -> str:
    """What the research agent is told this campaign will run on. Facts only:
    dataset, fills, commission, caps. Never whether an edge exists and never
    anything the correction computes -- the same rule as llm.LLMAgent's."""
    c = cfg()
    profile = c["profiles"][NQ_PROFILE]
    return (
        f"Dataset: real NQ futures trades, {N_SESSIONS} sessions on the "
        f"pre-registered training split.\n\n"
        f"Fill model: {profile['fill_model']} -- a fill requires a trade "
        "strictly through your limit, so strategies pay the spread on every "
        "entry and exit.\n\n"
        f"Commission: {profile['commission_per_contract']} index points per "
        "contract per side. NQ is $20 a point, so a round trip costs about "
        "$4.00 a contract and turnover is not free.\n\n"
        f"Action cap: {profile.get('action_cap', 64)} per session. "
        f"History window: {profile.get('history_window', 512)} bars.\n")


_STOP = frozenset("""
    the a an and or but nor of in on at to for with from by into onto about
    between among through during before after over under above below more most
    less least very just too also as than so then now again once here there
    when where while which who whom whose what why how whether not no only even
    still yet both all any some each every few other another such same own
    our your their its this that these those i we you he she it they me him
    her us them my mine hers ours yours theirs do does did doing done is are
    was were been being be have has had having will would shall should can
    could may might must am
    """.split())


def _default_query(brief_text: str) -> str:
    """Default arXiv query: the brief's most frequent content bigram.

    Measured live on the API while building this: a bare multi-term `all:`
    query is OR (noise), a long quoted sentence-phrase is an exact match that
    never occurs (nothing), AND-ing several generic singletons pulls in papers
    whose words coincide but whose topics do not (physics), and appending a
    frequent filler like "open" halves the hits. The form that returned the
    most on-topic papers for this repo's own brief was the single most frequent
    two-word collocation -- "intraday momentum" -- so that is the default.
    Headings are excluded (a "# Title" repeats the body and would win the
    frequency vote).

    It is still a default, not a law: `--query` overrides it, and `retrieve`
    prints the query so the human can see and veto it before the set is frozen.
    """
    import re
    from collections import Counter

    body = " ".join(line for line in brief_text.splitlines()
                    if line.lstrip() and not line.lstrip().startswith("#"))
    flat = re.sub(r"[#*_`{}\[\]()|:\\/0-9]+", " ", body).lower()
    tokens = [w for w in re.findall(r"[a-z]{3,}", flat) if w not in _STOP]
    counts = Counter(tokens)
    bigrams = Counter(f"{tokens[i]} {tokens[i + 1]}"
                      for i in range(len(tokens) - 1))
    if bigrams:
        best, best_n = None, -1
        for gram in bigrams:                 # first occurrence wins ties
            if bigrams[gram] > best_n:
                best, best_n = gram, bigrams[gram]
        return f'all:"{best}"'
    if counts:
        return f'all:"{counts.most_common(1)[0][0]}"'
    return 'all:"intraday"'


def _brief_path(name: str) -> pathlib.Path:
    p = RESEARCH_DIR / f"{name}.md"
    if not p.is_file():
        sys.exit(f"no research brief at {p}. Write one under config/research/ "
                 "first: the brief is the direction of interest, and the "
                 "retrieval is answered from it.")
    return p


def _retrieval_path(name: str) -> pathlib.Path:
    return RETRIEVALS_DIR / f"{name}.json"


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# stage: retrieve
# --------------------------------------------------------------------------- #

def stage_retrieve(name: str, query: str | None, max_results: int,
                   fulltext_papers: int, fulltext_chars: int) -> None:
    from src.core.loop.research import retrieve

    brief = _brief_path(name)
    query = query or _default_query(brief.read_text())
    print(f"brief      {brief}")
    print(f"query      {query}")
    print(f"searching arxiv for up to {max_results} results, "
          f"full text for the top {fulltext_papers} ...", flush=True)
    t0 = time.monotonic()
    rs = retrieve(name, query=query, max_results=max_results,
                  fulltext_papers=fulltext_papers,
                  fulltext_chars=fulltext_chars)
    digest = rs.write(_retrieval_path(name))
    print(f"retrieved  {len(rs.papers)} papers in "
          f"{time.monotonic() - t0:.0f}s")
    for i, p in enumerate(rs.papers[:max_results]):
        src = "ar5iv" if p.fulltext_src else "abstract only"
        print(f"  [{i}] {p.title}")
        print(f"      arxiv:{p.arxiv_id}  {p.published}  ({src})  by "
              + ", ".join(p.authors[:4]))
    print(f"\nfrozen     {_retrieval_path(name)}  digest {digest[:16]}")
    print("The set is written once. Fresh results need a fresh name; the old "
          "file stays on record.")
    print("\nNext: review the preview above, then `draft`.")


# --------------------------------------------------------------------------- #
# stage: draft
# --------------------------------------------------------------------------- #

def stage_draft(name: str, preflight: bool) -> None:
    from src.core.loop import hypothesis as H
    from src.core.loop.research import ResearchAgent, RetrievalSet

    brief = _brief_path(name)
    retrieval_path = _retrieval_path(name)
    if not retrieval_path.is_file():
        sys.exit(f"no retrieval at {retrieval_path} -- run `retrieve` first")
    retrieval, rdigest = RetrievalSet.load(retrieval_path)

    out = RESEARCH_RUNS / name
    out.mkdir(parents=True, exist_ok=True)

    agent = ResearchAgent(
        model=RESEARCH_MODEL, temperature=RESEARCH_TEMPERATURE,
        max_repair_attempts=2, brief=brief.read_text(), retrieval=retrieval,
        run_facts=research_run_facts())
    if preflight:
        served = agent.preflight()
        print(f"preflight   asked {served['requested']!r}  served "
              f"{served['served']!r}")

    print(f"drafting a hypothesis from {len(retrieval.papers)} papers ...",
          flush=True)
    t0 = time.monotonic()
    try:
        text = agent.draft()
    except Exception as exc:      # ModelSubstituted and ResearchDraftRejected
        (out / "draft.failed.log").write_text(
            f"{dt.datetime.now(dt.timezone.utc).isoformat()}\n{exc}\n")
        raise SystemExit(f"draft failed: {exc}")
    log = agent.drain_log()
    print(f"  draft in {time.monotonic() - t0:.0f}s  "
          f"{log['n_calls']} calls, {log['repair_attempts']} repairs, "
          f"{log['prompt_tokens']:,} in / {log['completion_tokens']:,} out")
    print(f"  models   {log['models_served']}")

    draft_file = out / "hypothesis.draft.md"
    draft_file.write_text(text)
    (out / "research.draft.json").write_text(json.dumps({
        "name": name,
        "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "brief_path": str(brief.relative_to(ROOT)),
        "brief_digest": _digest(brief),
        "retrieval_path": str(retrieval_path.relative_to(ROOT)),
        "retrieval_digest": rdigest,
        "config": agent.config(),
        "draft_digest": hashlib.sha256(text.encode()).hexdigest(),
        "draft_chars": len(text),
        "lint": H.findings(text),
        "calls": log,
    }, indent=2, sort_keys=True))
    print(f"\nhypothesis draft  {draft_file}")
    print(f"research record   {out / 'research.draft.json'}")
    print("\nNext steps, by hand:")
    print(f"  1. read the draft -- it must pass the gate-vocabulary lint that "
          f"guards it (check `lint` above is empty)")
    print(f"  2. save it as {HYPOTHESES_DIR / (name + '.md')} (edit only what "
          "you can defend as yours, not the market direction)")
    print("  3. commit the brief, the retrieval and the hypothesis together -- "
          "their commit date is the pre-registration")


# --------------------------------------------------------------------------- #
# stage: register
# --------------------------------------------------------------------------- #

def _require_committed(path: pathlib.Path) -> None:
    rel = str(path.relative_to(ROOT))
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", rel],
        cwd=ROOT, capture_output=True)
    if tracked.returncode != 0:
        sys.exit(f"{rel} is not tracked by git. The commit date of these files "
                 "is the pre-registration date; commit it before registering.")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", rel],
        cwd=ROOT, capture_output=True, text=True)
    if status.stdout.strip():
        sys.exit(f"{rel} has uncommitted changes. The campaign fights over "
                 "the committed file; the working copy must equal it.")


def _research_config(name: str, brief: pathlib.Path,
                     retrieval) -> dict:
    """The research agent's configuration, as recorded.

    The draft's record is the authority -- it is what actually wrote the
    hypothesis. A campaign registered without a draft record is registered
    against a reconstructed config, and the note says so, but that is a stall:
    the discipline is to draft, then register.
    """
    rec = RESEARCH_RUNS / name / "research.draft.json"
    if rec.is_file():
        blob = json.loads(rec.read_text())
        if (blob.get("brief_digest") == _digest(brief)
                and blob.get("retrieval_digest") == retrieval.digest()):
            return blob["config"]
    from src.core.loop.research import ResearchAgent

    agent = ResearchAgent(model=RESEARCH_MODEL,
                          temperature=RESEARCH_TEMPERATURE,
                          brief=brief.read_text(), retrieval=retrieval,
                          run_facts=research_run_facts())
    cfg_ = agent.config()
    cfg_["note"] = "reconstructed at registration; no draft record matched"
    return cfg_


def stage_register(name: str, campaign_id: str, note: str) -> None:
    from src.core.loop import hypothesis as H
    from src.core.loop.campaign import CampaignRecord
    from src.core.loop.research import RetrievalSet
    from src.core.loop.llm import LLMAgent

    brief = _brief_path(name)
    retrieval_path = _retrieval_path(name)
    hypothesis_path = HYPOTHESES_DIR / f"{name}.md"

    if not retrieval_path.is_file():
        sys.exit(f"no retrieval at {retrieval_path} -- run `retrieve` first")
    if not hypothesis_path.is_file():
        sys.exit(f"no hypothesis at {hypothesis_path} -- review the draft and "
                 "save it there, then commit it with the brief and retrieval")

    for p in (brief, retrieval_path, hypothesis_path):
        _require_committed(p)
    print("  committed and clean: brief, retrieval, hypothesis")

    retrieval, rdigest = RetrievalSet.load(retrieval_path)
    hypothesis = H.load(hypothesis_path)          # lints, digests
    print(f"  hypothesis lint clean; digests computed")

    d = RUNS / campaign_id
    if (d / "campaign.json").exists():
        sys.exit(f"{d / 'campaign.json'} already exists. A pre-registration is "
                 "written once; delete the directory deliberately if you mean "
                 "to start over.")

    # Preflight the proposing model the same way nq_campaign.py does, so the
    # record does not say one model while trials run on another.
    served = LLMAgent(model=MODEL, temperature=0.7, max_tokens=16_384).preflight()
    print(f"  preflight   asked {served['requested']!r}  served "
          f"{served['served']!r}")

    cc = cfg()["campaign"]
    research_cfg = _research_config(name, brief, retrieval)
    research_cfg["source"] = {
        "brief_path": str(brief.relative_to(ROOT)),
        "brief_digest": _digest(brief),
        "retrieval_path": str(retrieval_path.relative_to(ROOT)),
        "retrieval_digest": rdigest,
    }
    agent_cfg = LLMAgent(model=MODEL, temperature=0.7, max_tokens=16_384,
                         show_history=True, hypothesis=hypothesis).config()
    agent_cfg["null_source"] = cc["null_source"]
    agent_cfg["correction"] = cc["correction"]
    agent_cfg["b_null"] = 10_000
    agent_cfg["model_served_at_registration"] = served["served"]

    record = CampaignRecord(
        campaign_id=campaign_id, profile=NQ_PROFILE, budget=cc["n_trials"],
        q=cc["q"], agent="src.core.loop.llm.LLMAgent", dataset="nq-trades",
        split="train",
        hypothesis_path=str(hypothesis_path.relative_to(ROOT)),
        hypothesis_digest=hypothesis.digest,
        research_prompt_path=str(brief.relative_to(ROOT)),
        research_prompt_digest=_digest(brief),
        retrieval_path=str(retrieval_path.relative_to(ROOT)),
        retrieval_digest=rdigest,
        research_config=research_cfg,
        agent_config=agent_cfg,
        stopping_rule=(
            f"fixed budget of {cc['n_trials']}; no early stop on any observed "
            "result, no extension if the last trial looks promising"),
        note=(note or f"research-driven campaign from "
              f"{brief.relative_to(ROOT)}, answered from the frozen retrieval "
              f"{retrieval_path.relative_to(ROOT)}. The proposing agent is "
              "shown only the linted hypothesis, never the papers. Real NQ "
              "train split; governed by docs/design/pre-registration.md."),
        written_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    d.mkdir(parents=True, exist_ok=True)
    digest = record.write(d / "campaign.json")
    print(f"  registered {campaign_id}  N={cc['n_trials']}  q={cc['q']}  "
          f"null_source={cc['null_source']}  correction={cc['correction']}")
    print(f"  hypothesis   {hypothesis_path.relative_to(ROOT)}   "
          f"{hypothesis.digest[:16]}")
    print(f"  retrieval    {retrieval_path.relative_to(ROOT)}   {rdigest[:16]}")
    print(f"  digest       {digest}")
    print("\nNext:")
    print(f"  python scripts/nq_campaign.py --stage run    --name {campaign_id}")
    print(f"  python scripts/nq_campaign.py --stage nulls  --name {campaign_id}")
    print(f"  python scripts/nq_campaign.py --stage report --name {campaign_id}")


def _delegate(stage: str, campaign_id: str) -> None:
    """run/nulls/report are nq_campaign.py's stages; the record drives them."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import nq_campaign  # noqa: E402  (run/nulls/report live in one place)

    {"run": nq_campaign.stage_run, "nulls": nq_campaign.stage_nulls,
     "report": nq_campaign.stage_report}[stage](campaign_id)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True,
                    choices=("retrieve", "draft", "register", "run", "nulls",
                             "report"),
                    help="retrieve|draft|register are this script's stages; "
                         "run|nulls|report are handed to "
                         "scripts/nq_campaign.py.")
    ap.add_argument("--name", default=None,
                    help="the research brief under config/research/<name>.md")
    ap.add_argument("--campaign-id", default=None,
                    help="for register/run/nulls/report: the campaign id, e.g. "
                         "research-intraday-01")
    ap.add_argument("--query", default=None,
                    help="arxiv search query; defaults to the brief's most "
                         "frequent two-word collocation, e.g. "
                         'all:"intraday momentum"')
    ap.add_argument("--max-results", type=int, default=10)
    ap.add_argument("--fulltext-papers", type=int, default=3)
    ap.add_argument("--fulltext-chars", type=int, default=16_000)
    ap.add_argument("--preflight", action="store_true",
                    help="draft: one cheap endpoint call to check substitution")
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)

    if a.stage == "retrieve":
        if not a.name:
            ap.error("--name (the brief under config/research/<name>.md) is "
                     "required for retrieve")
        stage_retrieve(a.name, a.query, a.max_results, a.fulltext_papers,
                       a.fulltext_chars)
    elif a.stage == "draft":
        if not a.name:
            ap.error("--name is required for draft")
        stage_draft(a.name, a.preflight)
    elif a.stage in ("register", "run", "nulls", "report"):
        if not a.campaign_id:
            ap.error(f"--campaign-id is required for {a.stage}")
        if a.stage == "register":
            if not a.name:
                ap.error("--name (the research brief) is required for register")
            stage_register(a.name, a.campaign_id, a.note)
        else:
            _delegate(a.stage, a.campaign_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())