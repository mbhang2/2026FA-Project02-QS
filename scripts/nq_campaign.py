#!/usr/bin/env python3
"""The MVP campaign: a model proposes, the harness scores, the gate decides.

    python scripts/nq_campaign.py --stage register   # preflight, pre-register, hash
    python scripts/nq_campaign.py --stage run        # N trials, resumable
    python scripts/nq_campaign.py --stage nulls      # sign-flip null per candidate
    python scripts/nq_campaign.py --stage report     # gate, summary, tier 3

Governed by `docs/design/pre-registration.md`. Settings come from
`config/run.yaml` under `campaign:` and from the named profile -- **nothing about
the evaluation is an argument to this script**, for the reason `run.py`'s
docstring gives at length.

**This is a Karpathy loop and the earlier stub campaigns were not.** Task 52 and
53 scored 60 strategies that already existed. Here the model proposes one at a
time, is shown every prior trial's score, and -- new in Task 56 -- **edits the
champion rather than re-deriving from a fixed baseline**. The file on disk
advances when a trial beats the best so far, which is `git reset` semantics and
what the autoquant literature does.

Four stages because the run is hours and a crash in hour three should not cost
hours one and two. `register` refuses to overwrite: the record is the
pre-registration and rewriting it is the failure this project exists to prevent.

**Nothing here shows the agent the gate.** No threshold, no p-value, no N, no
verdict. The correction happens in `report`, after every trial is in the ledger.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

DEFAULT_PROFILE = "nq-train"
OUT = ROOT / "runs" / "campaigns"
MODEL = "claude-haiku-4-5"
DEFAULT_HYPOTHESIS = ROOT / "config" / "hypotheses" / "intraday-continuation.md"
B_NULL = 10_000

# The `CampaignRecord.dataset` label. `profile["dataset"]` names the run.yaml
# dataset block (`nq`, `xauusd`); the record labels the adapters' own names
# (`nq-trades`, `xauusd`), which is what the calendar path and `summarise()`
# derive from. Kept explicit rather than built by constructing a dataset, which
# would demand R2 credentials at `register` time.
DATASET_RECORD_NAMES = {"nq": "nq-trades", "xauusd": "xauusd"}


def cfg() -> dict:
    return yaml.safe_load((ROOT / "config" / "run.yaml").read_text())


def campaign_cfg() -> dict:
    c = cfg()["campaign"]
    if c["correction"] in ("max_of_n", "step_down_max") and \
            c["null_source"] not in ("conditional", "round_turn_mc"):
        sys.exit(
            f"incoherent configuration: correction={c['correction']!r} needs "
            f"per-candidate null samples, but null_source={c['null_source']!r} "
            "produces none. See pre-registration.md section 3a. Failing at "
            "registration rather than at trial 40.")
    return c


def run_facts(profile: dict, n_sessions: int) -> str:
    return (
        f"Dataset: {profile.get('instrument', 'real market data')}, "
        f"{n_sessions} sessions on the pre-registered {profile['split']} split.\n\n"
        f"Fill model: {profile['fill_model']} -- a fill requires a price move "
        "strictly through your limit, so you pay the spread on every entry and "
        "exit.\n\n"
        f"Commission: {profile['commission_per_contract']} per unit per side. "
        f"{profile.get('cost_note', '')}\n\n"
        f"Action cap: {profile.get('action_cap', 64)} per session. "
        f"History window: {profile.get('history_window', 512)} bars.\n")


def agent_for(champion: pathlib.Path | None, hypothesis: pathlib.Path,
              profile_name: str):
    from src.core.loop import hypothesis as H
    from src.core.loop.llm import LLMAgent

    c = cfg()
    profile = c["profiles"][profile_name]
    return LLMAgent(
        model=MODEL, temperature=0.7, max_tokens=16_384, show_history=True,
        hypothesis=H.load(hypothesis), champion=champion,
        run_facts=run_facts(profile, profile.get("fact_sessions", 0)))


# --------------------------------------------------------------------------- #

def stage_register(name: str, hypothesis: pathlib.Path, profile_name: str) -> None:
    from src.core.loop.campaign import CampaignRecord
    from src.core.loop.llm import LLMAgent

    d = OUT / name
    if (d / "campaign.json").exists():
        sys.exit(f"{d / 'campaign.json'} already exists. A pre-registration is "
                 "written once.")

    print("preflight ...", flush=True)
    served = LLMAgent(model=MODEL, temperature=0.7, max_tokens=16_384).preflight()
    print(f"  asked {served['requested']!r}  served {served['served']!r}")

    cc, profile = campaign_cfg(), cfg()["profiles"][profile_name]
    dataset_name = DATASET_RECORD_NAMES.get(profile["dataset"], profile["dataset"])
    agent_cfg = agent_for(None, hypothesis, profile_name).config()
    agent_cfg["null_source"] = cc["null_source"]
    agent_cfg["correction"] = cc["correction"]
    agent_cfg["b_null"] = B_NULL
    agent_cfg["model_served_at_registration"] = served["served"]
    agent_cfg["commission_per_contract"] = profile["commission_per_contract"]

    d.mkdir(parents=True, exist_ok=True)
    record = CampaignRecord(
        campaign_id=name, profile=profile_name, budget=cc["n_trials"], q=cc["q"],
        agent="src.core.loop.llm.LLMAgent", dataset=dataset_name,
        split=profile["split"],
        hypothesis_path=str(hypothesis.relative_to(ROOT)),
        hypothesis_digest=agent_cfg["hypothesis"]["digest"],
        agent_config=agent_cfg,
        note=("MVP campaign, governed by docs/design/pre-registration.md. Real "
              f"{dataset_name} {profile['split']} split. The model proposes one "
              "strategy at a time and edits the champion; the null is the "
              "sign-flip over round turns (section 3b), NOT the conditional "
              "null, so the measured FWER of 0.060 does not describe this "
              "campaign."),
        written_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    digest = record.write(d / "campaign.json")
    print(f"  registered {name}  N={cc['n_trials']}  q={cc['q']}  "
          f"null_source={cc['null_source']}  correction={cc['correction']}")
    print(f"  digest {digest}")


def stage_run(name: str) -> None:
    from src.core.loop.campaign import CampaignRecord, run_trial

    d = OUT / name
    record, _ = CampaignRecord.read(d / "campaign.json")
    # The hypothesis is read back from the pre-registered record, not from a
    # fresh CLI flag: `run` must investigate what `register` committed, so a
    # re-run can never silently run a different question than the one hashed
    # into the record.
    hyp = ROOT / record.hypothesis_path if record.hypothesis_path \
        else DEFAULT_HYPOTHESIS
    ledger = d / "ledger.jsonl"
    champion = d / "champion.py"
    state = d / "state.json"

    rows = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()] \
        if ledger.exists() else []
    best = json.loads(state.read_text())["best"] if state.exists() else None
    start = len(rows)
    if start >= record.budget:
        print(f"{name}: all {record.budget} trials already run")
        return
    print(f"{name}: resuming at trial {start} of {record.budget}", flush=True)

    agent = agent_for(champion if champion.exists() else None, hyp, record.profile)
    t0 = time.monotonic()
    for i in range(start, record.budget):
        agent.champion = champion if champion.exists() else None
        source = agent.propose(rows)
        row = run_trial(source, profile=record.profile, ledger=ledger,
                        campaign_id=record.campaign_id, index=i)
        drain = getattr(agent, "drain_log", None)
        if drain is not None:
            # Kept in a sidecar, because `run.py` owns the ledger row and this is
            # the driver's cost rather than the run's. The first version attached
            # it to the in-memory row only, so the campaign's bill was lost the
            # moment the process exited.
            row["agent_call"] = drain()
            with (d / "agent-calls.jsonl").open("a") as fh:
                fh.write(json.dumps({"trial": i, **row["agent_call"]}) + "\n")

        # The champion advances only on a strict improvement. A tie keeps the
        # incumbent, so the base does not drift between equivalent files.
        score = (row.get("score") or {}).get("primary")
        rejected = (row.get("score") or {}).get("rejected")
        if score is not None and not rejected and (best is None or score > best):
            best = score
            champion.write_text(source)
            state.write_text(json.dumps({"best": best, "trial": i}))
        row["champion_best_after"] = best
        rows.append(row)

        el = time.monotonic() - t0
        done = i - start + 1
        print(f"  {i + 1}/{record.budget}  outcome={row.get('outcome')} "
              f"score={score if score is None else round(score, 4)}  "
              f"best={best if best is None else round(best, 4)}  "
              f"{el / 60:.0f}m elapsed, eta {(el / done) * (record.budget - i - 1) / 60:.0f}m",
              flush=True)
    print(f"{name}: {record.budget} trials in {(time.monotonic() - t0) / 60:.0f} min")


def stage_nulls(name: str) -> None:
    """The sign-flip null per candidate. Seconds, not hours -- section 3b."""
    from src.core.engine.reconstruct import reconstruct
    from src.core.scoring import metrics
    from src.core.scoring.randomization import round_turn as rt

    sys.path.insert(0, str(ROOT / "scripts"))
    from replay import load_fill_log  # noqa: E402

    d = OUT / name
    if (d / "nulls.json").exists():
        print(f"{name}: nulls already computed")
        return
    record, _ = CampaignRecord.read(d / "campaign.json")
    rows = [json.loads(x) for x in (d / "ledger.jsonl").read_text().splitlines() if x.strip()]
    profile = cfg()["profiles"][record.profile]
    commission = profile["commission_per_contract"]

    # The same dataset the trials ran on, built the same way `run.py` builds it,
    # with the profile's seed -- so the session calendar the null is reassembled
    # onto is the one the metric used, not a lookalike.
    import run as runner
    from src.core.types import Split

    dataset = runner.build_dataset(cfg(), profile["dataset"], profile["seed"])
    sessions = dataset.scored_sessions(Split.TRAIN,
                                       profile.get("warmup_sessions", 0))
    calendar = metrics.session_calendar(dataset, sessions)
    print(f"  calendar: {len(calendar)} scored sessions on the train split",
          flush=True)

    samples, t0 = [], time.monotonic()
    for i, row in enumerate(rows):
        fills = load_fill_log(row)
        score = row.get("score") or {}
        if not fills or score.get("primary") is None or score.get("rejected"):
            samples.append([])
            continue
        blotter = reconstruct(fills, commission_per_contract=commission)
        samples.append(rt.null_sample(blotter, calendar, b=B_NULL, seed=70_000 + i))
        print(f"  {i + 1}/{len(rows)}  {len(samples[-1])} draws  "
              f"{time.monotonic() - t0:.0f}s", flush=True)
    (d / "nulls.json").write_text(json.dumps(
        {"source": "round_turn_mc", "b": B_NULL, "samples": samples,
         "spec": "docs/design/pre-registration.md section 3b"}))
    print(f"{name}: nulls in {time.monotonic() - t0:.0f}s")


def stage_report(name: str) -> None:
    from src.core.loop.campaign import CampaignRecord, CampaignResult, summarise

    d = OUT / name
    record, digest = CampaignRecord.read(d / "campaign.json")
    rows = [json.loads(x) for x in (d / "ledger.jsonl").read_text().splitlines() if x.strip()]
    samples = json.loads((d / "nulls.json").read_text())["samples"]

    result = CampaignResult(record=record, record_digest=digest, rows=rows)
    summary = summarise(result, dataset=record.dataset, null_samples=samples, q=record.q)

    # `campaign_summary.py` reads `result.json`, so the rows reach tier 2 by the
    # same route every other campaign's do rather than through this script.
    (d / "result.json").write_text(json.dumps(
        {"rows": rows, "wall_clock_s": result.wall_clock_s,
         "record_digest": digest, "tampered": result.tampered},
        indent=2, default=str))
    summary["null_source"] = "round_turn_mc"
    summary["error_control_note"] = (
        "The FWER of 0.060, CI [0.041, 0.088], is the CONDITIONAL null with "
        "max_of_n at R = 150 (Task 38). It does not describe this campaign, "
        "whose null is the sign-flip over round turns. See "
        "docs/design/pre-registration.md sections 3a and 3b.")
    (d / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    g = summary.get("gate", {})
    print(f"  N {summary['n_trials']}  outcomes {summary['outcomes']}")
    print(f"  observed best {g.get('observed_best')}   null max mean "
          f"{g.get('null_max_mean')}")
    print(f"  p {g.get('p')}   reject-the-null {g.get('reject')}   "
          f"best>0 {g.get('observed_best_positive')}   ACCEPT {g.get('accept')}")

    # **Tier 3 reads `tier2-summary.json`, not `summarise()`'s output.** The
    # contract is a file and the two shapes are not the same -- feeding the
    # wrong one made tier 3 report "the best candidate's source is absent",
    # which was true of the object it was handed and not of the campaign.
    import subprocess
    subprocess.run([sys.executable, str(ROOT / "scripts" / "campaign_summary.py"),
                    str(d)], cwd=str(ROOT), check=True)

    tier2 = json.loads((d / "tier2-summary.json").read_text())
    from audit.tier3 import audit_campaign_summary
    verdict = audit_campaign_summary(tier2)
    (d / "tier3-verdict.json").write_text(
        json.dumps(verdict.as_dict(), indent=2, default=str))
    print(f"  tier 3: {verdict.verdict.value}  escalate "
          f"{verdict.escalate_to_human}  can_promote False")
    for r in verdict.reasons:
        print(f"    - {r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True,
                    choices=("register", "run", "nulls", "report"))
    ap.add_argument("--name", required=True)
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    help="run.yaml profile the campaign searches on "
                         "(default: nq-train). Locked into the record at "
                         "`register` and read back for `run`/`nulls`/`report`.")
    ap.add_argument("--hypothesis", type=pathlib.Path, default=DEFAULT_HYPOTHESIS,
                    help="hypothesis file locked in at `register` time, and read "
                         "back from the record for `run` (default: "
                         "config/hypotheses/intraday-continuation.md)")
    a = ap.parse_args()
    if a.stage == "register":
        stage_register(a.name, a.hypothesis, a.profile)
    elif a.stage == "run":
        stage_run(a.name)  # hypothesis + profile re-read from the record
    elif a.stage == "nulls":
        stage_nulls(a.name)
    else:
        stage_report(a.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
