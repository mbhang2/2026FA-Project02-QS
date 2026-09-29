#!/usr/bin/env python3
"""The corpus pipeline: the arXiv wash in, a pre-registered NQ campaign out.

    python scripts/research_pipeline.py research --prompt "..." [--query Q]
    python scripts/research_pipeline.py fetch    [--prompt P] [--query Q] [--max-pages N] [--per-page K] [--delay S] [--dry-run]
    python scripts/research_pipeline.py screen   [--arxiv-id ID] [--preflight] [--profile P] [--n-sessions K]
    python scripts/research_pipeline.py rerank   [--arxiv-id ID] [--prompt P]
    python scripts/research_pipeline.py distil   [--arxiv-id ID] [--preflight] [--fulltext-chars C] [--no-fulltext]
    python scripts/research_pipeline.py register --arxiv-id ID --name NAME --campaign-id ID [--note N]
    python scripts/research_pipeline.py view     [--arxiv-id ID] [--json]

`scripts/research_campaign.py` answers one brief: freeze a search, draft,
register. This pipeline is the feed's answer to the question "what should we be
investigating at all". Two ways in, one registry:

  * `research --prompt "..."` -- the prompt is both question and lens: fetch
    derives the arXiv query from it (its most frequent content bigram; `--query`
    overrides), screens, re-ranks, and distils in one go, scoped to the papers
    that fetch just registered. For "I have a question", this is the command.
  * `fetch` with no prompt -- walk the q-fin feed newest-first, no question in
    mind, and let the screens/de-motes surface whatever deserves a look.

Way in makes no difference further down: the registry remembers every paper it
has seen (`src/core/loop/registry.py`), screens each one against the desk's
data contract, demotes the families already falsified on this data, distils the
survivors into one hypothesis page each, and registers the human-endorsed
candidate as a campaign -- the same write-once record `research_campaign.py`
writes.

  1. `fetch`    -- `--prompt` derives the query (printed before anything is
      fetched); no prompt walks the feed (q-fin.TR/ST/CP, newest first, metadata
      only). Every paper lands under runs/corpus/papers/<base-id>.json. A paper
      re-encountered is a no-op: the row that answered first is the row that
      stays.
  2. `screen`   -- for every paper without a verdict: the paper's title and
      abstract are read against `data_contract()` -- the same config/run.yaml
      the harness reads -- and the screen agent answers direct | adaptable |
      infeasible. An `adaptable` verdict must bind an adaptation; the verdict
      is recorded once.
  3. `rerank`   -- model-free. Demote the falsified families from
      `docs/datasets/nq-es-qqq.md` (with a citation in the reason, never a
      silent drop) and score relevance against the prompt when one is given,
      else the human-maintained `config/research/open-questions.md`.
  4. `distil`   -- for every surviving, un-demoted paper: fetch its full text
      from ar5iv (tier-2, bounded) and run the distil agent to a hypothesis
      page -- linted against the same gate vocabulary as any committed
      hypothesis and bounded to 800-1,500 tokens. Drafts land in
      runs/corpus/distills/<base-id>.md, human-visible.
  5. `register` -- the human has saved the reviewed draft as
      `config/hypotheses/<name>.md`; register bakes it (plus the brief, the
      distill draft, prompt, and the campaign facts) into a write-once
      campaign record. `run`/`nulls`/`report` are nq_campaign.py's, unchanged.
  6. `view`     -- the registry, as per-paper states or raw event lines.

**Nothing here runs the gate.** No threshold, no p-value, no N, no correction;
`report` does all of that later, from the record.

**The registry is deliberately not a fingerprinted artifact.** It decides
*which question* gets asked; the question is frozen as the committed hypothesis
with its digest in the campaign record. One frozen thing per campaign is
enough.
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

CONFIG_RESEARCH = ROOT / "config" / "research"
HYPOTHESES_DIR = ROOT / "config" / "hypotheses"
DISTILLS_DIR = ROOT / "runs" / "corpus" / "distills"

MODEL = "claude-haiku-4-5"        # the proposing LLMAgent, as in nq_campaign.py
DISTIL_MODEL = "claude-haiku-4-5"
DISTIL_TEMPERATURE = 0.3
SCREEN_TEMPERATURE = 0.2
NG_SESSIONS = 384
NQ_PROFILE = "nq-train"


def _distills_dir(a) -> pathlib.Path:
    """Drafts live beside their registry, so `--root` keeps the run self-contained."""
    return pathlib.Path(a.root) / "distills"


def _hypothesis_rel(path: pathlib.Path) -> str:
    """Repo-relative when the draft is under ROOT (committed paths read better);
    absolute otherwise -- a `--root` run outside the tree still records a path
    that resolves."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _resolve_hypothesis(rel: str) -> pathlib.Path:
    p = pathlib.Path(rel)
    if p.is_absolute():
        return p
    return ROOT / p


def open_questions_text() -> str:
    from src.core.loop.research import open_questions

    return open_questions()


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry(args) -> "Registry":
    from src.core.loop.registry import Registry

    return Registry(root=pathlib.Path(args.root))


def _paper_from_row(row: dict) -> "Paper":
    from src.core.loop.research import Paper

    return Paper(
        arxiv_id=row["arxiv_id"], title=row["title"],
        authors=tuple(row["authors"]), published=row["published"],
        abstract=row["abstract"], categories=tuple(row["categories"]),
        fulltext=row.get("fulltext", ""), fulltext_src=row.get("fulltext_src", ""))


def _row_papers(reg, keys: list[str]) -> list[tuple[str, "Paper"]]:
    out = []
    for key in keys:
        row = reg._load_paper(key)
        if row is None:
            sys.exit(f"corpus has no paper row for {key}; re-fetch it")
        out.append((key, _paper_from_row(row)))
    return out


# --------------------------------------------------------------------------- #
# stage: fetch
# --------------------------------------------------------------------------- #

def stage_fetch(a) -> list[str]:
    from src.core.loop.research import FEED_QUERY, default_query, fetch_feed

    reg = _registry(a)
    if a.query:
        query = a.query
    elif a.prompt:
        query = default_query(a.prompt)
        print(f"prompt   {a.prompt[:160]}")
    else:
        query = FEED_QUERY
    print(f"feed    {query}")
    print(f"fetching up to {a.max_pages} pages of {a.per_page}, "
          f"{a.delay}s between pages ...", flush=True)
    papers = fetch_feed(query=query, per_page=a.per_page, max_pages=a.max_pages,
                        delay_s=a.delay)
    new_keys: list[str] = []
    new = known = 0
    for paper in papers:
        if a.dry_run:
            print(f"  [would register] {paper.arxiv_id}  {paper.title}")
            continue
        created, row = reg.register(paper, query)
        new += created
        known += not created
        if created:
            new_keys.append(row["key"])
        print(f"  {'+' if created else '='} {paper.arxiv_id}  {paper.title}")
    print(f"\nseen {len(papers)} papers: {new} new rows, {known} already "
          f"registered, under {reg.root}")
    if a.dry_run:
        print("dry-run: nothing was written")
    else:
        print("\nNext: `screen` the papers without a verdict.")
    return new_keys


# --------------------------------------------------------------------------- #
# stage: research -- the whole chain for one prompt
# --------------------------------------------------------------------------- #

def stage_research(a) -> None:
    """One prompt, one run: fetch what it implies, screen, re-rank, distil.

    The chain is scoped to the papers *this* fetch just registered, so an old
    feed crawl in the same corpus does not leak into a prompt's run. Each stage
    is still a first-class command; `research` is the "I have a question in
    mind" convenience that calls them in order. Registration stays human: the
    chain stops at the distilled pages and prints the next commands.
    """
    if not a.prompt:
        sys.exit("research requires --prompt (or use the individual stages, "
                 "which accept --prompt on rerank/distil and --query on fetch)")
    keys = stage_fetch(a)
    if a.dry_run:
        return
    if not keys:
        print("nothing new for this prompt: every paper it returned was "
              "already in the corpus. `view` shows what has been decided.")
        return
    stage_screen(a, keys)
    stage_rerank(a, keys)
    stage_distil(a, keys)
    reg = _registry(a)
    gone = [st for st in (reg.state(k) for k in keys)
            if st.get("demoted") is True or "feasibility" not in st]
    distilled = [st for st in (reg.state(k) for k in keys)
                 if "hypothesis_path" in st]
    print("\n-- prompt run complete --")
    print(f"  {len(keys)} paper(s): {len(distilled)} distilled, "
          f"{len(gone)} demoted or unscreened")
    for st in distilled:
        print(f"  candidate  {st['key']}  {st['hypothesis_path']}")
    print("Next, by hand: read a draft, save it as "
          "config/hypotheses/<name>.md, commit it, then:")
    print("  python scripts/research_pipeline.py register "
          "--arxiv-id <id> --name <name> --campaign-id research-<name>-01")


# --------------------------------------------------------------------------- #
# stage: screen
# --------------------------------------------------------------------------- #

def _scoped_keys(reg, stage, keys, a) -> list[str]:
    """The stage's own pending guard, applied even to an explicit key set.

    `research` hands every stage the exact papers its fetch just registered; a
    stage must still skip what its own predicate already decided (a demoted
    paper is never distilled, a screened paper is not re-screened)."""
    guard = set(reg.pending(stage))
    if keys is not None:
        return [k for k in keys if k in guard]
    if a.arxiv_id:
        return [a.arxiv_id] if a.arxiv_id in guard else []
    return sorted(guard)


def stage_screen(a, keys: list[str] | None = None) -> None:
    from src.core.loop.research import (
        ScreenAgent, ScreenRejected, data_contract)

    reg = _registry(a)
    contract = data_contract(profile=a.profile, n_sessions=a.n_sessions)
    keys = _scoped_keys(reg, "screen", keys, a)
    if not keys:
        print("nothing to screen")
        return
    print(f"contract {contract.facts['profile']} "
          f"({contract.facts['dataset']}, {contract.facts['n_sessions']} "
          f"sessions)")
    print(f"screening {len(keys)} paper(s) ...", flush=True)
    for key, paper in _row_papers(reg, keys):
        print(f"  {key}  {paper.title}", flush=True)
        agent = ScreenAgent(model=a.model, temperature=SCREEN_TEMPERATURE,
                            max_repair_attempts=2, paper=paper,
                            contract=contract.block())
        if a.preflight:
            served = agent.preflight()
            print(f"    preflight   asked {served['requested']!r}  served "
                  f"{served['served']!r}")
        t0 = time.monotonic()
        try:
            verdict = agent.screen()
        except ScreenRejected as exc:
            print(f"    screen failed: {exc}")
            print("    The verdict was never recorded; a re-screen is a decision "
                  "by hand. A screen that cannot parse is not the same as a "
                  "paper that was screened.")
            sys.exit("screen aborted: a verdict could not be produced")
        log = agent.drain_log()
        reg.append(kind="screen", arxiv_id=key,
                   **verdict.as_dict(),
                   **{f"config_{k}": v for k, v in agent.config().items()},
                   n_calls=log["n_calls"],
                   repair_attempts=log["repair_attempts"])
        print(f"    {verdict.feasibility}  ({time.monotonic() - t0:.0f}s, "
              f"{log['n_calls']} calls)  {verdict.reason[:120]}")
        if verdict.adaptation:
            print(f"      adaptation: {verdict.adaptation[:120]}")
    print("\nNext: `rerank` to demote the falsified families and score "
          "relevance.")


# --------------------------------------------------------------------------- #
# stage: rerank
# --------------------------------------------------------------------------- #

def stage_rerank(a, keys: list[str] | None = None) -> None:
    from src.core.loop.research import rerank

    reg = _registry(a)
    oq = open_questions_text()
    keys = _scoped_keys(reg, "rerank", keys, a)
    if not keys:
        print("nothing to re-rank")
        return
    positive = a.prompt or oq
    if a.prompt:
        print(f"prompt   {a.prompt[:160]}")
    elif oq:
        print(f"open questions  {len(oq)} chars")
    else:
        print("no prompt and no config/research/open-questions.md -- demotion "
              "only, relevance skipped")
    for key, paper in _row_papers(reg, keys):
        verdict = rerank(paper, open_questions_text=oq, prompt_text=a.prompt or "")
        reg.append(kind="rerank", arxiv_id=key, **verdict.as_dict(),
                   prompt=a.prompt or "")
        mark = "DEMOTE " if verdict.demoted else "keep   "
        rel = "n/a" if verdict.relevance is None else verdict.relevance
        print(f"  {mark} {key}  relevance {rel}"
              + ("  " + verdict.demoted_reason if verdict.demoted else ""))
    print("\nNext: `distil` the survivors. Demoted papers stop here.")


# --------------------------------------------------------------------------- #
# stage: distil
# --------------------------------------------------------------------------- #

def stage_distil(a, keys: list[str] | None = None) -> None:
    from src.core.loop import hypothesis as H
    from src.core.loop.rejection import (
        RejectionUndisclosed, RejectionUngrounded, check as check_rejection,
        duty)
    from src.core.loop.research import (
        DISTIL_FULLTEXT_CHARS, DATA_DOC_PATH, DistilAgent, DistillRejected,
        data_contract, fetch_fulltext)

    reg = _registry(a)
    keys = _scoped_keys(reg, "distil", keys, a)
    if not keys:
        print("nothing to distil")
        return
    # KB-2, this desk's version. What the corpus already ruled out, so a page
    # cannot re-propose a trial that ran. The text goes into the *prompt* only
    # as "already ruled out on this data" -- the same shape as the falsified
    # families -- never as a campaign summary, and never into a vector store.
    # `DistilAgent` then refuses a page that fails to cite a key that is real.
    # `d` is built here, before any page is read, so a page cannot narrow the
    # obligation it is measured against.
    d = duty(reg)
    duty_block = d.prompt_block()
    if d.rejected:
        print(f"prior rejections to disclose: {len(d.rejected)}")
    brief = a.prompt or open_questions_text()
    contract_block = data_contract(profile=a.profile, n_sessions=a.n_sessions).block()
    data_doc = DATA_DOC_PATH.read_text()
    distills = _distills_dir(a)
    distills.mkdir(parents=True, exist_ok=True)
    print(f"distilling {len(keys)} survivor(s) ...", flush=True)
    for key, paper in _row_papers(reg, keys):
        full = paper
        if not a.no_fulltext:
            full = fetch_fulltext(paper, limit_chars=a.fulltext_chars)
        print(f"  {key}  {paper.title}", flush=True)
        agent = DistilAgent(
            model=a.model, temperature=DISTIL_TEMPERATURE,
            max_repair_attempts=2, paper=full, brief=brief,
            run_facts=contract_block, data_doc=data_doc,
            prior_rejections=duty_block)
        if a.preflight:
            served = agent.preflight()
            print(f"    preflight   asked {served['requested']!r}  served "
                  f"{served['served']!r}")
        t0 = time.monotonic()
        try:
            text = agent.distil()
            # The second disclosure, checked on the file rather than inside the
            # agent: the page must name a rejection that resolves. With no
            # rejections in the corpus there is nothing to disclose and this
            # returns empty. `d` was built before the page was read.
            cited_rejections = check_rejection(text, d)
        except (DistillRejected, RejectionUndisclosed, RejectionUngrounded) as exc:
            (distills / f"{key}.failed.log").write_text(
                f"{dt.datetime.now(dt.timezone.utc).isoformat()}\n{exc}\n")
            print(f"    distil failed: {exc}")
            sys.exit("distil aborted: a survivor could not be distilled")
        log = agent.drain_log()
        md = distills / f"{key}.md"
        record = {
            "paper_key": key,
            "brief_open_questions_digest": (_digest(CONFIG_RESEARCH / "open-questions.md")
                                            if (CONFIG_RESEARCH / "open-questions.md").is_file()
                                            else None),
            "data_doc_path": str(DATA_DOC_PATH.relative_to(ROOT)),
            "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "config": agent.config(),
            "draft_digest": hashlib.sha256(text.encode()).hexdigest(),
            "draft_chars": len(text),
            "draft_tokens_est": len(text) // 4,
            "lint": H.findings(text),
            "prior_rejections_cited": [r.key for r in cited_rejections],
            "calls": log,
        }
        md.write_text(text)
        (distills / f"{key}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n")
        reg.append(kind="distil", arxiv_id=key,
                   hypothesis_path=_hypothesis_rel(md),
                   draft_digest=record["draft_digest"],
                   draft_tokens_est=record["draft_tokens_est"],
                   draft_chars=record["draft_chars"],
                   **{f"config_{k}": v for k, v in agent.config().items()},
                   n_calls=log["n_calls"],
                   repair_attempts=log["repair_attempts"])
        print(f"    written {_hypothesis_rel(md)}  "
              f"{record['draft_chars']} chars, ~{record['draft_tokens_est']} "
              f"tokens, {record['lint'] == []} lint-clean  "
              f"({log['n_calls']} calls, {time.monotonic() - t0:.0f}s)")
    print("\nNext, by hand: read runs/corpus/distills/<id>.md, save it as "
          "config/hypotheses/<name>.md, commit it, then `register`.")


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


def stage_register(a) -> None:
    from src.core.loop import hypothesis as H
    from src.core.loop.campaign import CampaignRecord
    from src.core.loop.llm import LLMAgent

    reg = _registry(a)
    st = reg.state(a.arxiv_id)
    if "hypothesis_path" not in st:
        sys.exit(f"{a.arxiv_id} has no distil event; nothing has been distilled")
    if "campaign_id" in st:
        sys.exit(f"{a.arxiv_id} is already registered as {st['campaign_id']}; "
                 "a decision is made once")

    hypothesis_path = HYPOTHESES_DIR / f"{a.name}.md"
    if not hypothesis_path.is_file():
        sys.exit(f"no hypothesis at {hypothesis_path} -- review the distill "
                 "draft runs/corpus/distills/<id>.md and save it there")

    draft_path = _resolve_hypothesis(st["hypothesis_path"])
    oq_path = CONFIG_RESEARCH / "open-questions.md"
    commit_candidates = [hypothesis_path, draft_path] + ([oq_path] if oq_path.is_file() else [])
    for p in commit_candidates:
        _require_committed(p)
    print("  committed and clean: hypothesis, distill draft"
          + (", open questions" if oq_path.is_file() else ""))

    hypothesis = H.load(hypothesis_path)          # lints, digests
    print(f"  hypothesis lint clean; digests computed")

    d = ROOT / "runs" / "campaigns" / a.campaign_id
    if (d / "campaign.json").exists():
        sys.exit(f"{d / 'campaign.json'} already exists. A pre-registration is "
                 "written once; delete the directory deliberately if you mean "
                 "to start over.")

    served = LLMAgent(model=MODEL, temperature=0.7, max_tokens=16_384).preflight()
    print(f"  preflight   asked {served['requested']!r}  served "
          f"{served['served']!r}")

    cc = yaml.safe_load((ROOT / "config" / "run.yaml").read_text())["campaign"]
    # The corpus research config is the distill record -- the agent that wrote
    # the draft is the agent that wrote this hypothesis's ancestor. The record
    # is named by base id, like every registry row.
    from src.core.loop.registry import key_of

    record_path = _distills_dir(a) / f"{key_of(a.arxiv_id)}.json"
    if record_path.is_file():
        research_cfg = json.loads(record_path.read_text())["config"]
    else:
        research_cfg = {"note": "no distill record matched; this should not "
                                "happen after a distil event"}
    brief = oq_path if oq_path.is_file() else None
    research_cfg["source"] = {
        "corpus_key": a.arxiv_id,
        "open_questions_path": (str(brief.relative_to(ROOT))
                                if brief is not None else None),
        "open_questions_digest": (_digest(brief) if brief is not None else None),
        "distill_draft_path": str(draft_path.relative_to(ROOT)),
        "distill_draft_digest": _digest(draft_path),
    }
    agent_cfg = LLMAgent(model=MODEL, temperature=0.7, max_tokens=16_384,
                         show_history=True, hypothesis=hypothesis).config()
    agent_cfg["null_source"] = cc["null_source"]
    agent_cfg["correction"] = cc["correction"]
    agent_cfg["b_null"] = 10_000
    agent_cfg["model_served_at_registration"] = served["served"]

    record = CampaignRecord(
        campaign_id=a.campaign_id, profile=NQ_PROFILE, budget=cc["n_trials"],
        q=cc["q"], agent="src.core.loop.llm.LLMAgent", dataset="nq-trades",
        split="train",
        hypothesis_path=str(hypothesis_path.relative_to(ROOT)),
        hypothesis_digest=hypothesis.digest,
        research_prompt_path=(str(brief.relative_to(ROOT))
                              if brief is not None else None),
        research_prompt_digest=(_digest(brief) if brief is not None else None),
        retrieval_path=None,
        retrieval_digest=None,
        research_config=research_cfg,
        agent_config=agent_cfg,
        stopping_rule=(
            f"fixed budget of {cc['n_trials']}; no early stop on any observed "
            "result, no extension if the last trial looks promising"),
        note=(a.note or f"corpus-driven campaign from the distill of "
              f"{a.arxiv_id} (runs/corpus/distills/). The proposing agent is "
              "shown only the linted hypothesis, never the paper. Real NQ "
              "train split; governed by docs/design/pre-registration.md."),
        written_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    d.mkdir(parents=True, exist_ok=True)
    digest = record.write(d / "campaign.json")
    reg.append(kind="register", arxiv_id=a.arxiv_id,
               campaign_id=a.campaign_id, name=a.name,
               campaign_digest=digest)
    print(f"  registered {a.campaign_id}  N={cc['n_trials']}  q={cc['q']}  "
          f"null_source={cc['null_source']}  correction={cc['correction']}")
    print(f"  corpus     {a.arxiv_id} -> {hypothesis_path.relative_to(ROOT)}"
          f"   {hypothesis.digest[:16]}")
    print(f"  digest     {digest}")
    print("\nNext:")
    print(f"  python scripts/nq_campaign.py --stage run    --name {a.campaign_id}")
    print(f"  python scripts/nq_campaign.py --stage nulls  --name {a.campaign_id}")
    print(f"  python scripts/nq_campaign.py --stage report --name {a.campaign_id}")


# --------------------------------------------------------------------------- #
# stage: view
# --------------------------------------------------------------------------- #

def stage_view(a) -> None:
    reg = _registry(a)
    if a.json:
        if a.arxiv_id:
            print(json.dumps(reg.state(a.arxiv_id), indent=2, sort_keys=True))
        else:
            print(json.dumps({
                "papers": reg.states(),
                "pending": {s: reg.pending(s)
                            for s in ("screen", "rerank", "distil",
                                      "register")},
            }, indent=2, sort_keys=True))
        return
    rows = [reg.state(a.arxiv_id)] if a.arxiv_id else reg.states()
    if not rows:
        print("the corpus is empty; run `fetch` first")
        return
    for st in rows:
        stage = ("distilled" if "hypothesis_path" in st
                 else "demoted" if st.get("demoted") is True
                 else "reranked" if "demoted" in st
                 else "screened" if "feasibility" in st
                 else "fetched")
        feat = st.get("feasibility") or "-"
        camp = st.get("campaign_id") or "-"
        print(f"{st['key']}  {st.get('title', '')[:50]}")
        print(f"    {stage}  verdict={feat}  demoted={st.get('demoted', '-')}  "
              f"relevance={st.get('relevance', '-')}  campaign={camp}")
        if st.get("hypothesis_path"):
            print(f"    hypothesis {st['hypothesis_path']}")
    if not a.arxiv_id:
        pend = {s: reg.pending(s) for s in ("screen", "rerank", "distil",
                                            "register")}
        print("\npending  " + "   ".join(f"{s}={len(v)}"
                                         for s, v in pend.items()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=("research", "fetch", "screen", "rerank",
                                      "distil", "register", "view"))
    ap.add_argument("--prompt", default=None,
                    help="the research question. research: required (it is the "
                         "lens). fetch: derives the arXiv query when --query is "
                         "absent. rerank/distil: the positive signal/brief "
                         "instead of open-questions.md.")
    ap.add_argument("--arxiv-id", default=None,
                    help="work only this paper (base id, version optional)")
    ap.add_argument("--root", default=None,
                    help="registry root (default runs/corpus)")
    ap.add_argument("--model", default=DISTIL_MODEL,
                    help="screen/distil model (default claude-haiku-4-5)")
    ap.add_argument("--preflight", action="store_true",
                    help="one cheap endpoint call before the first agent call")

    fetch = ap.add_argument_group("fetch")
    fetch.add_argument("--query", default=None)
    fetch.add_argument("--per-page", type=int, default=100)
    fetch.add_argument("--max-pages", type=int, default=3)
    fetch.add_argument("--delay", type=float, default=3.0)
    fetch.add_argument("--dry-run", action="store_true")

    screen = ap.add_argument_group("screen")
    screen.add_argument("--profile", default=NQ_PROFILE,
                        help="profiles key in config/run.yaml")
    screen.add_argument("--n-sessions", type=int, default=NG_SESSIONS)

    distil = ap.add_argument_group("distil")
    distil.add_argument("--fulltext-chars", type=int, default=80_000)
    distil.add_argument("--no-fulltext", action="store_true")

    register = ap.add_argument_group("register")
    register.add_argument("--name", default=None,
                          help="register: hypothesis name under "
                               "config/hypotheses/<name>.md")
    register.add_argument("--campaign-id", default=None)
    register.add_argument("--note", default="")

    view = ap.add_argument_group("view")
    view.add_argument("--json", action="store_true",
                      help="view: raw states/pending as JSON")

    a = ap.parse_args(argv)
    if a.root is None:
        a.root = str(ROOT / "runs" / "corpus")

    if a.stage == "research":
        stage_research(a)
    elif a.stage == "fetch":
        stage_fetch(a)
    elif a.stage == "screen":
        stage_screen(a)
    elif a.stage == "rerank":
        stage_rerank(a)
    elif a.stage == "distil":
        stage_distil(a)
    elif a.stage == "register":
        if not a.arxiv_id:
            ap.error("register requires --arxiv-id")
        if not a.name:
            ap.error("register requires --name (the hypothesis under "
                     "config/hypotheses/<name>.md)")
        if not a.campaign_id:
            ap.error("register requires --campaign-id")
        stage_register(a)
    elif a.stage == "view":
        stage_view(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())