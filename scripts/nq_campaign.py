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

PROFILE = "nq-train"
OUT = ROOT / "runs" / "campaigns"
MODEL = "claude-haiku-4-5"
# The default only applies to `--stage register`, which writes the choice into
# campaign.json. Every later stage reads the path back out of the record, so a
# campaign cannot be registered against one question and run against another.
DEFAULT_HYPOTHESIS = ROOT / "config" / "hypotheses" / "intraday-continuation.md"
B_NULL = 10_000


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
        f"Dataset: real NQ futures trades, {n_sessions} sessions on the "
        f"pre-registered {profile['split']} split.\n\n"
        f"Fill model: {profile['fill_model']} -- a fill requires a trade "
        "strictly through your limit, so you pay the spread on every entry and "
        "exit.\n\n"
        f"Commission: {profile['commission_per_contract']} index points per "
        "contract per side. NQ is $20 a point, so a round trip costs about "
        "$4.00 a contract. At high turnover this dominates: the baseline "
        "strategy on this split traded 103,355 times and paid more in "
        "commission than it made gross.\n\n"
        f"Action cap: {profile.get('action_cap', 64)} per session. "
        f"History window: {profile.get('history_window', 512)} bars.\n")


def agent_for(champion: pathlib.Path | None, hypothesis: pathlib.Path | None):
    from src.core.loop import hypothesis as H
    from src.core.loop.llm import LLMAgent

    c = cfg()
    profile = c["profiles"][PROFILE]
    return LLMAgent(
        model=MODEL, temperature=0.7, max_tokens=16_384, show_history=True,
        # None is a real state, not a missing argument: `hypothesis_path` is
        # optional on the record, and a campaign that registered without a
        # question runs without one rather than borrowing another campaign's.
        hypothesis=H.load(hypothesis) if hypothesis is not None else None,
        champion=champion, run_facts=run_facts(profile, 384))


# --------------------------------------------------------------------------- #

VALIDATION_PROFILE = "nq-validation"


def _winner(rows: list[dict]) -> dict | None:
    """The campaign's gate winner: the highest primary score among completed runs.

    The same candidate `summarise` calls best, by the same rule, so validation
    cannot quietly check a different strategy from the one the gate ruled on.
    """
    scored = [r for r in rows if isinstance(r.get("score"), dict)
              and r["score"].get("primary") is not None]
    return max(scored, key=lambda r: r["score"]["primary"]) if scored else None


def stage_validate(name: str) -> None:
    """Run the campaign's winner once on the validation split (section 5).

    **This is not a trial and must never be counted as one.** N is the number of
    looks taken while *searching*, and the winner was already chosen on train --
    running it again on held-out data does not search for anything. It goes to a
    separate ledger with `--purpose check`, so `global_n()` cannot pick it up.

    The multiplicity that validation *does* create -- M campaigns each getting one
    validation look -- is corrected at L2 by `max_of_m` (section 4), not here.

    Section 5's three rules are computed and all three recorded: (a) Sortino > 0
    and (b) the L2 test are pass conditions; (c) train-versus-validation decay is
    **evidence for the committee and not a threshold**, because "within X%" needs
    an X and this project does not fit one.
    """
    import subprocess

    from src.core.loop.campaign import CampaignRecord

    d = OUT / name
    record, _ = CampaignRecord.read(d / "campaign.json")
    rows = [json.loads(x) for x in (d / "ledger.jsonl").read_text().splitlines()
            if x.strip()]
    win = _winner(rows)
    if win is None:
        sys.exit(f"{name}: no completed trial has a score; nothing to validate.")

    digest = win.get("strategy_digest")
    src_file = d / "sources" / f"{digest}.py"
    source = src_file.read_text() if src_file.exists() else win.get("strategy_source")
    if not source:
        sys.exit(f"{name}: the winner's source is not on disk (digest {digest}).")

    train_score = win["score"]["primary"]
    print(f"{name}: validating winner  digest {digest}  train {train_score:+.4f}")

    out = d / "validation.jsonl"
    before = len(out.read_text().splitlines()) if out.exists() else 0
    if before:
        sys.exit(
            f"{out} already has {before} row(s). The validation split is looked at "
            "ONCE per campaign (section 5). Re-running would be a second look that "
            "the L2 correction does not know about -- delete the file deliberately "
            "if you really mean to discard the first look and record why.")

    strategy_path = ROOT / "src" / "strategies" / "current.py"
    original = strategy_path.read_text()
    strategy_path.write_text(source)
    try:
        proc = subprocess.run(
            [sys.executable, "run.py", "--human", "--quiet",
             "--purpose", "check", "--profile", VALIDATION_PROFILE,
             "--ledger", str(out)],
            capture_output=True, text=True, cwd=str(ROOT))
    finally:
        strategy_path.write_text(original)

    vrows = [json.loads(x) for x in out.read_text().splitlines() if x.strip()] \
        if out.exists() else []
    if not vrows:
        sys.exit(f"{name}: the validation run produced no ledger row.\n"
                 f"{proc.stderr[-2000:]}")
    v = vrows[-1]
    if v.get("strategy_digest") != digest:
        sys.exit(
            f"{name}: validation ran digest {v.get('strategy_digest')} but the "
            f"winner is {digest}. Refusing to record a comparison between two "
            "different strategies.")

    vscore = (v.get("score") or {}).get("primary")
    result = {
        "schema": "campaign-validation/1",
        "campaign_id": name,
        "winner_digest": digest,
        "winner_campaign_index": win.get("campaign_index"),
        "train": {"profile": record.profile, "primary": train_score,
                  "score": win.get("score"), "diagnostics": win.get("diagnostics")},
        "validation": {"profile": VALIDATION_PROFILE, "primary": vscore,
                       "outcome": v.get("outcome"), "score": v.get("score"),
                       "diagnostics": v.get("diagnostics"),
                       "git_sha": v.get("git_sha")},
        "rule_a_sortino_positive": (vscore is not None and vscore > 0),
        "rule_c_decay": (None if vscore is None else vscore - train_score),
        "note": ("Section 5. Rule (a) Sortino > 0 and rule (b) the max_of_m test "
                 "at L2 are pass conditions; rule (c), the train-to-validation "
                 "change, is committee evidence and NOT a threshold -- it is "
                 "reported unjudged, including when validation beats train. This "
                 "run has purpose 'check' and is not a trial: it does not count "
                 "toward N. The multiplicity of one validation look per campaign "
                 "is corrected at L2 by max_of_m, not here."),
    }
    (d / "validation.json").write_text(json.dumps(result, indent=2, default=str))
    if vscore is None:
        print(f"  validation did not score: outcome {v.get('outcome')}")
    else:
        print(f"  train {train_score:+.4f}   validation {vscore:+.4f}   "
              f"change {vscore - train_score:+.4f}")
        print(f"  rule (a) Sortino > 0 on validation: {result['rule_a_sortino_positive']}")
    print(f"  wrote {d / 'validation.json'}")


# --------------------------------------------------------------------------- #

def stage_register(name: str, hypothesis: pathlib.Path) -> None:
    from src.core.loop.campaign import CampaignRecord
    from src.core.loop.llm import LLMAgent

    d = OUT / name
    if (d / "campaign.json").exists():
        sys.exit(f"{d / 'campaign.json'} already exists. A pre-registration is "
                 "written once.")

    print("preflight ...", flush=True)
    served = LLMAgent(model=MODEL, temperature=0.7, max_tokens=16_384).preflight()
    print(f"  asked {served['requested']!r}  served {served['served']!r}")

    cc, profile = campaign_cfg(), cfg()["profiles"][PROFILE]
    agent_cfg = agent_for(None, hypothesis).config()
    agent_cfg["null_source"] = cc["null_source"]
    agent_cfg["correction"] = cc["correction"]
    agent_cfg["b_null"] = B_NULL
    agent_cfg["model_served_at_registration"] = served["served"]
    agent_cfg["commission_per_contract"] = profile["commission_per_contract"]
    # Amendment 3. In the record because the campaign is scored with it, so a
    # reader cannot tell what the numbers mean without it -- and because the
    # record digest then covers it, which is what stops it changing mid-campaign
    # the way the hypothesis cannot.
    from src.core.scoring.metrics import IDLE_CHARGE

    agent_cfg["idle_charge"] = cc.get("idle_charge", IDLE_CHARGE)

    d.mkdir(parents=True, exist_ok=True)
    record = CampaignRecord(
        campaign_id=name, profile=PROFILE, budget=cc["n_trials"], q=cc["q"],
        agent="src.core.loop.llm.LLMAgent", dataset="nq-trades", split="train",
        hypothesis_path=str(hypothesis.relative_to(ROOT)),
        hypothesis_digest=agent_cfg["hypothesis"]["digest"],
        agent_config=agent_cfg,
        note=("MVP campaign, governed by docs/design/pre-registration.md. Real "
              "NQ train split. The model proposes one strategy at a time and "
              "edits the champion; the null is the sign-flip over round turns "
              "(section 3b), NOT the conditional null. The conditional null's "
              "FWER of 0.060 therefore does not describe this campaign; the "
              "figure that does is Task 57's, measured on this exact "
              "configuration at this exact N: 0 acceptances in 400 campaigns, "
              "FWER below about 0.010 at 95%. It is conservative, and Task 59 "
              "prices that -- 80% power at a signal strength of 0.287 against "
              "0.242 for the conditional null."),
        written_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    digest = record.write(d / "campaign.json")
    print(f"  registered {name}  N={cc['n_trials']}  q={cc['q']}  "
          f"null_source={cc['null_source']}  correction={cc['correction']}")
    print(f"  digest {digest}")


def stage_run(name: str) -> None:
    from src.core.loop.campaign import CampaignRecord, run_trial

    d = OUT / name
    record, _ = CampaignRecord.read(d / "campaign.json")
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

    # **A killed trial leaves the candidate behind.** `run_trial` restores
    # `current.py` in a `finally`, which a SIGKILL does not run -- and on this
    # machine the campaign HAS been killed mid-trial, by the memory watchdog. The
    # next trial would then read the abandoned candidate as its "original" and
    # restore that afterwards, so the reference file silently becomes whatever the
    # agent last wrote and stays that way for the rest of the campaign.
    #
    # Restore it from the commit rather than trusting what is on disk. Checked
    # every resume, because the whole point is that the previous process did not
    # get to clean up after itself.
    import subprocess as _sp

    cur = "src/strategies/current.py"
    dirty = _sp.run(["git", "status", "--porcelain", "--", cur],
                    capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()
    if dirty:
        _sp.run(["git", "checkout", "--", cur], cwd=str(ROOT), check=True)
        print(f"  restored {cur} from HEAD -- a previous trial was killed before "
              "it could put the reference strategy back", flush=True)

    # From the record, never from a flag or the module default: a resumed run
    # must ask the question the pre-registration names. The digest is re-checked
    # because `hypothesis_path` is a path -- editing the file it points at would
    # otherwise change the question silently, halfway through a campaign.
    from src.core.loop import hypothesis as H

    hyp_path = ROOT / record.hypothesis_path if record.hypothesis_path else None
    got = H.load(hyp_path).digest if hyp_path else None
    if got != record.hypothesis_digest:
        sys.exit(
            f"{hyp_path} has changed since {name} was registered.\n"
            f"  registered digest {record.hypothesis_digest}\n"
            f"  current digest    {got}\n"
            "A campaign answers the question it registered. Restore the file, or "
            "register a new campaign for the new question.")

    agent = agent_for(champion if champion.exists() else None, hyp_path)
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
    rows = [json.loads(x) for x in (d / "ledger.jsonl").read_text().splitlines() if x.strip()]
    profile = cfg()["profiles"][PROFILE]
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
    summary = summarise(result, dataset="nq-trades", null_samples=samples, q=record.q)

    # `campaign_summary.py` reads `result.json`, so the rows reach tier 2 by the
    # same route every other campaign's do rather than through this script.
    (d / "result.json").write_text(json.dumps(
        {"rows": rows, "wall_clock_s": result.wall_clock_s,
         "record_digest": digest, "tampered": result.tampered},
        indent=2, default=str))
    summary["null_source"] = "round_turn_mc"
    summary["error_control_note"] = (
        "This campaign's null is the sign-flip over round turns, not the "
        "conditional null, so Task 38's FWER of 0.060 CI [0.041, 0.088] does "
        "NOT describe it. The figure that does is Task 57's, measured on this "
        "configuration (round_turn_mc x max_of_n, q = 0.05, B = 10,000) at both "
        "N = 10 and N = 50: 0 acceptances in 400 campaigns per arm, FWER below "
        "about 0.010 at 95%, below 0.005 pooled over 800. It is conservative "
        "rather than merely correct, and Task 59 prices the conservatism: 80% "
        "power at a signal strength of 0.287 against 0.242 for the conditional "
        "null. See docs/design/pre-registration.md sections 3a and 3b.")
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

    # **The committee is a separate file, not merged into the tier-3 verdict.**
    # Tier 3 is a deterministic audit of the record: same input, same output,
    # re-runnable by anyone. The committee is four language models reading the
    # same evidence, and its answer can differ between runs of the same campaign.
    # Folding an unreproducible judgement into a reproducible one would make the
    # combined artefact neither, and `ledger_check` could no longer tell a
    # changed verdict from a changed apparatus.
    if record.hypothesis_path:
        hyp_text = (ROOT / record.hypothesis_path).read_text()
    else:
        hyp_text = None
    # Section 5's out-of-sample evidence, if `--stage validate` has run. Absent is
    # a state the slices state explicitly rather than omit -- see
    # `_validation_evidence`: "not checked" and "checked and held up" are opposite
    # readings and a silent gap gets read as the flattering one.
    vfile = d / "validation.json"
    if vfile.exists():
        tier2["validation"] = json.loads(vfile.read_text())
        print(f"  validation: train {tier2['validation']['train']['primary']:+.4f}"
              f"  validation {tier2['validation']['validation']['primary']:+.4f}")
    else:
        print("  validation: not run (--stage validate)")

    best_source = (tier2.get("best_candidate") or {}).get("source")
    print("  committee ...", flush=True)
    try:
        from audit.committee.review import review
        cv = review(tier2, source=best_source, model=MODEL, hypothesis=hyp_text)
    except Exception as exc:                                      # noqa: BLE001
        # Recorded, not swallowed. A committee that failed to run is a different
        # state from one that had nothing to veto, and the MVP rule "no veto =
        # approve" would read the second from the first.
        (d / "committee-verdict.json").write_text(json.dumps(
            {"schema": "committee-verdict/1", "verdict": "ERROR",
             "error": f"{type(exc).__name__}: {exc}",
             "note": ("The committee did not run. This is NOT a pass: the MVP "
                      "rule reads 'no veto' as approval, and no verdict is not "
                      "no veto.")}, indent=2))
        print(f"  committee FAILED: {type(exc).__name__}: {exc}")
        print("    recorded as ERROR in committee-verdict.json -- not a pass")
        return
    (d / "committee-verdict.json").write_text(
        json.dumps(cv.as_dict(), indent=2, default=str))
    print(f"  committee: {cv.verdict}"
          + (f"  vetoed by {', '.join(v.axis for v in cv.auditors if v.verdict == 'VETO')}"
             if cv.verdict == "VETO" else ""))
    for v in cv.auditors:
        print(f"    {v.axis:<12} {v.verdict}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True,
                    choices=("register", "run", "nulls", "validate", "report"))
    ap.add_argument("--name", required=True)
    ap.add_argument("--hypothesis", type=pathlib.Path,
                    default=DEFAULT_HYPOTHESIS,
                    help="path to the hypothesis markdown; --stage register only, "
                         "and it is written into campaign.json. Later stages read "
                         "it from there and ignore this flag.")
    a = ap.parse_args()
    if a.stage == "register":
        hyp = a.hypothesis.resolve()
        try:
            hyp.relative_to(ROOT)
        except ValueError:
            sys.exit(f"{hyp} is outside the repository. A campaign's question has "
                     "to be a committed file, or the record points at nothing a "
                     "reader can check.")
        stage_register(a.name, hyp)
    else:
        # Refused rather than ignored. Silently accepting it on a run stage is how
        # someone believes they changed the question and did not.
        if a.hypothesis.resolve() != DEFAULT_HYPOTHESIS.resolve():
            sys.exit(f"--hypothesis applies to --stage register only; "
                     f"--stage {a.stage} reads it from campaign.json.")
        {"run": stage_run, "nulls": stage_nulls,
         "validate": stage_validate, "report": stage_report}[a.stage](a.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
