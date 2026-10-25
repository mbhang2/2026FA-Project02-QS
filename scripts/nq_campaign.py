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
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

PROFILE = "nq-train"
# **Amendment 13: where campaign output goes.** `runs/` unless a rehearsal
# redirects it. `runlog.ledger_paths()` reads `runs/ledger.jsonl` and
# `runs/campaigns/**` and nothing else, so a rehearsal writing under `.scratch/`
# is invisible to `global_n()` by construction rather than by a purpose filter
# anyone could forget -- the same argument that makes Amendment 12's worker
# ledgers safe. Read from the environment because the session driver and the
# campaign driver are separate processes.
import os as _os
_RUNS = pathlib.Path(_os.environ.get("PRACTICUM_RUNS_ROOT") or (ROOT / "runs"))
OUT = _RUNS / "campaigns"
# **Not haiku, and docs/setup/MODEL-ACCESS.md says why in one line:**
# `claude-haiku-4-5` passes a small prompt and is substituted to
# `gemini-3.1-pro-preview` at our real proposal prompt of ~167,000 characters.
# `ModelSubstituted` catches the swap and refuses the trial, so no record was
# ever corrupted -- but it means a campaign registered on the default model
# cannot run at all, which is what happened to the first a12 proof registration
# on 2026-10-04. The preflight at register time passes, because the preflight
# prompt is small; the first real proposal then dies.
#
# deepseek-v4-pro is what the last nine campaigns actually used, named in their
# plans rather than taken from here.
MODEL = "deepseek-v4-pro"
# The default only applies to `--stage register`, which writes the choice into
# campaign.json. Every later stage reads the path back out of the record, so a
# campaign cannot be registered against one question and run against another.
DEFAULT_HYPOTHESIS = ROOT / "config" / "hypotheses" / "intraday-continuation.md"
# **The proposer's token budget has to cover the model's REASONING, not just
# the file it writes.** nq-ml-05 lost 4 of its first 10 trials to "no python
# block in the reply" at 16,384: gemini-3.1-pro-preview spends ~12,000 tokens
# thinking before its first visible token, so a successful call used 14,994 of
# 16,384 and any call whose reasoning ran 10% long truncated before the code
# fence closed. Third instance of one defect -- the committee client at 4,096
# against deepseek, the preflight at 16, this.
#
# 64,000 is ~4x the largest reasoning budget measured and the replies are a few
# thousand tokens, so the ceiling stops being a variable. It is recorded in
# every campaign record, so a campaign run under a different one is visible.
#
# **It was 65,536 until 2026-10-20, and that silently cost every trial.**
# `claude-haiku-4-5` tops out at 64,000 output tokens. The gateway does not
# refuse a larger ask -- it falls through its wildcard route,
# `fallbacks=[{'*': ['gemini-3.1-pro-preview']}]`, to a model that accepts it,
# and answers HTTP 200 naming gemini. Bisected on the live endpoint:
#
#     49,152 -> claude-haiku-4-5        64,000 -> claude-haiku-4-5
#     63,488 -> claude-haiku-4-5        64,256 -> gemini-3.1-pro-preview
#     65,536 -> gemini-3.1-pro-preview  65,537 -> HTTP 400 from Vertex AI
#
# The 400 at 65,537 is the proof it had routed to Google. So the constant was
# one step over a cliff it could not see, and `ModelSubstituted` fired on trial
# 0 of every rehearsal. Task 36 and Task 37 were never affected: both run at
# 16,384, which is what their records say.
PROPOSER_MAX_TOKENS = 64_000

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
    """The operational facts of *this* run, for the proposing agent.

    **It must describe the run the agent is actually in.** A fitted campaign
    withholds the first `fit_sessions` of the split for training and scores only
    what is left, so an agent told the split's full length is told the wrong
    number -- and an agent never told a fit window exists will not use one,
    because `strategy-api.md` 7c says a `fit()` proposal is refused unless the
    campaign registered it. Neither failure raises; both just produce ordinary
    strategies in a campaign that registered for something else.
    """
    fit = int(profile.get("fit_sessions", 0) or 0)
    scored = n_sessions - fit
    fitted_block = ""
    if fit:
        fitted_block = (
            f"**This is a fitted campaign.** The first {fit} sessions of the "
            f"split are a FIT WINDOW: bars are delivered and your orders are "
            f"ignored, exactly as during warmup, and your `fit()` is called once "
            f"at the edge of it. You are scored on the remaining {scored} "
            f"sessions only, which you have never seen when `fit()` runs. You "
            f"may define `fit`, `save_weights` and `load_weights` -- see section "
            f"7c of the API document above.\n\n"
            f"Fewer scored sessions means a noisier score: {scored} against the "
            f"{n_sessions} an unfitted campaign gets is roughly "
            f"{(n_sessions / scored) ** 0.5:.2f}x the sampling error. That is "
            f"arithmetic about the window, not a comment on your proposal.\n\n")
    return (
        fitted_block
        + f"Dataset: real NQ futures trades, {scored} scored sessions on the "
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


def agent_for(champion: pathlib.Path | None, hypothesis: pathlib.Path | None,
              profile_name: str = PROFILE, model: str = MODEL):
    from src.core.loop import hypothesis as H
    from src.core.loop.llm import LLMAgent

    c = cfg()
    profile = c["profiles"][profile_name]
    return LLMAgent(
        model=model, temperature=0.7, max_tokens=PROPOSER_MAX_TOKENS, show_history=True,
        # None is a real state, not a missing argument: `hypothesis_path` is
        # optional on the record, and a campaign that registered without a
        # question runs without one rather than borrowing another campaign's.
        hypothesis=H.load(hypothesis) if hypothesis is not None else None,
        # 384 is the train split's length, not a constant: `run_facts`
        # subtracts the profile's fit window from it, so a fitted campaign's
        # agent is told the scored count rather than the split count.
        champion=champion, run_facts=run_facts(profile, 384))


# --------------------------------------------------------------------------- #

VALIDATION_PROFILE = "nq-validation"
VALIDATION_PROFILE_FITTED = "nq-validation-fitted"


def validation_profile_for(train_profile: str, fitted: bool) -> str:
    """The validation profile that matches the campaign's OWN train profile.

    **This was the two constants above, read unconditionally.** Harmless while
    every campaign was NQ and actively wrong as soon as one is not: a campaign
    registered on `spx-options-train` would have had its winner validated on
    `nq-validation` -- a different instrument, a different split of a different
    dataset -- and the comparison recorded as though the two numbers were about
    the same thing. Nothing would have raised; section 5 would simply have
    reported a replication failure that was really a configuration error.

    The rule is the naming convention the profiles already follow: `<base>-train`
    validates on `<base>-validation`, with `-fitted` carried through. The result
    is then CHECKED rather than trusted -- it must exist, it must actually be a
    validation split, and it must cost the same as train, because the protocol
    compares the two numbers directly and a cheaper split looks like a better
    strategy. That last one is the Task 61 bug, which reached a live run.
    """
    c = cfg()
    base = (train_profile[:-len("-train")] if train_profile.endswith("-train")
            else train_profile)
    want = f"{base}-validation" + ("-fitted" if fitted else "")
    if want not in c["profiles"]:
        sys.exit(
            f"no validation profile {want!r} for a campaign registered on "
            f"{train_profile!r}. A campaign is validated on the split that "
            f"matches its own training profile, so the pair has to exist in "
            f"config/run.yaml. Have: {sorted(c['profiles'])}")
    v, t = c["profiles"][want], c["profiles"][train_profile]
    if v.get("split") != "validation":
        sys.exit(f"profile {want!r} is not a validation split ({v.get('split')!r})")
    for key in ("dataset", "fill_model", "commission_per_contract"):
        if v.get(key) != t.get(key):
            sys.exit(
                f"{want!r} and {train_profile!r} disagree on {key}: "
                f"{v.get(key)!r} against {t.get(key)!r}. The protocol compares a "
                f"campaign's train and validation scores directly, and two runs "
                f"costed or fed differently are not comparable -- the cheaper "
                f"one simply looks better.")
    return want


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
    # Amendment 5. Rows written before it carry no uncharged figure; that is
    # recorded as None rather than recomputed here, because a validation
    # artefact should read the ledger, not re-derive it.
    train_unch = win["score"].get("primary_uncharged")
    print(f"{name}: validating winner  digest {digest}  train {train_score:+.4f}")

    out = d / "validation.jsonl"
    before = len(out.read_text().splitlines()) if out.exists() else 0
    if before:
        sys.exit(
            f"{out} already has {before} row(s). The validation split is looked at "
            "ONCE per campaign (section 5). Re-running would be a second look that "
            "the L2 correction does not know about -- delete the file deliberately "
            "if you really mean to discard the first look and record why.")

    # **A fitted champion is source AND weights; carrying only the source
    # carries half of it.** For an unfitted strategy the source IS the whole
    # strategy, which is why this stage only ever swapped `current.py`. A fitted
    # one learned its parameters on train, and a validation run that re-derived
    # them would be fitting on the split it is about to be scored against.
    #
    # So the weights travel the way the source does: a fixed path whose contents
    # are swapped in and removed again in the `finally`. The durable copy stays
    # in the campaign's `weights/` directory, addressed by digest; this is a
    # transient and is gitignored.
    #
    # The profile changes with it, because `weights_path` cannot sit on
    # `nq-validation` unconditionally -- the engine treats a weights file that is
    # not there as an error rather than as permission to refit.
    weights_rel = win.get("weights_source") or ""
    weights_src = (ROOT / weights_rel) if weights_rel else None
    if weights_src is not None and not weights_src.exists():
        sys.exit(
            f"{name}: the winner recorded weights at {weights_rel} and the file "
            "is not there. A fitted champion cannot be validated without the "
            "parameters it fitted, and refitting on the validation split is the "
            "one thing this stage exists to prevent.")

    profile = validation_profile_for(record.profile, bool(weights_src))
    live_weights = ROOT / "runs" / "current.weights"

    strategy_path = ROOT / "src" / "strategies" / "current.py"
    original = strategy_path.read_text()
    strategy_path.write_text(source)
    if weights_src:
        live_weights.parent.mkdir(parents=True, exist_ok=True)
        live_weights.write_bytes(weights_src.read_bytes())
        print(f"{name}: carrying weights from {weights_rel}")
    try:
        proc = subprocess.run(
            [sys.executable, "run.py", "--human", "--quiet",
             "--purpose", "check", "--profile", profile,
             "--ledger", str(out)],
            capture_output=True, text=True, cwd=str(ROOT))
    finally:
        strategy_path.write_text(original)
        live_weights.unlink(missing_ok=True)

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
    vscore_unch = (v.get("score") or {}).get("primary_uncharged")
    result = {
        "schema": "campaign-validation/1",
        "campaign_id": name,
        "winner_digest": digest,
        "winner_campaign_index": win.get("campaign_index"),
        "train": {"profile": record.profile, "primary": train_score,
                  "primary_uncharged": train_unch,
                  "score": win.get("score"), "diagnostics": win.get("diagnostics")},
        "validation": {"profile": profile, "primary": vscore,
                       "primary_uncharged": vscore_unch,
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
        if vscore_unch is not None:
            tu = f"{train_unch:+.4f}" if train_unch is not None else "n/a (row predates Amendment 5)"
            print(f"  without idle charge: train {tu}   validation {vscore_unch:+.4f}")
        print(f"  rule (a) Sortino > 0 on validation: {result['rule_a_sortino_positive']}")
    print(f"  wrote {d / 'validation.json'}")


# --------------------------------------------------------------------------- #

def _refuse_if_floor_drifted(name: str) -> None:
    """The floor in the record must still be the floor the scorer will apply.

    `run.py` reads `campaign.min_exposed_fraction` from `config/run.yaml`. That
    file is one line long and shared by every campaign, so a campaign
    registered at 0.70 can be run -- or worse, validated -- months later under
    somebody else's 0.50 and nothing in the ledger would say so. The record is
    the claim; this is the check that it is still true.
    """
    from src.core.loop.campaign import CampaignRecord

    path = OUT / name / "campaign.json"
    if not path.exists():
        return
    record, _ = CampaignRecord.read(path)
    registered = getattr(record, "min_exposed_fraction", None)
    configured = campaign_cfg().get("min_exposed_fraction")
    if registered != configured:
        sys.exit(
            f"{name} registered min_exposed_fraction {registered!r} and "
            f"config/run.yaml now says {configured!r}.\n"
            f"run.py scores from run.yaml, so continuing would run this "
            f"campaign under a floor its own record does not name. Set "
            f"campaign.min_exposed_fraction back to {registered!r} to continue "
            f"this campaign, or register a new one for the floor you want.")



def _campaign_note(profile: dict) -> str:
    """The record's prose. The statistical provenance is NOT portable.

    The note used to be one hard-coded paragraph claiming Task 57's measured
    FWER -- 0 acceptances in 400 campaigns, below about 0.010 at 95% -- and
    Task 59's power curve. Both are real numbers and both were measured on the
    NQ MVP configuration: that dataset, that null, that N. Carrying them onto a
    campaign with a different dataset would be asserting a false-positive rate
    nobody measured, in the field a reader goes to precisely to find out how
    much the result is worth. A campaign on anything else says so and says what
    is missing.
    """
    common = ("Governed by docs/design/pre-registration.md. The model proposes "
              "one strategy at a time and edits the champion; the null is the "
              "sign-flip over round turns (section 3b), NOT the conditional "
              "null.")
    if profile.get("dataset") == "nq":
        return (f"MVP campaign. {common} Real NQ train split. The conditional "
                "null's FWER of 0.060 therefore does not describe this "
                "campaign; the figure that does is Task 57's, measured on this "
                "exact configuration at this exact N: 0 acceptances in 400 "
                "campaigns, FWER below about 0.010 at 95%. It is conservative, "
                "and Task 59 prices that -- 80% power at a signal strength of "
                "0.287 against 0.242 for the conditional null.")
    return (f"{common} Dataset {profile['dataset']!r}, {profile['split']} "
            "split. **No measured FWER applies to this configuration.** Task "
            "57's 400-campaign figure and Task 59's power curve were measured "
            "on the NQ MVP configuration and are not transferable to a "
            "different dataset or a different null; quoting them here would be "
            "asserting a false-positive rate nobody has measured. Read an "
            "acceptance from this campaign as uncalibrated until its own null "
            "is tabulated.")

def stage_register(name: str, hypothesis: pathlib.Path,
                   profile_name: str = PROFILE,
                   seed: pathlib.Path | None = None,
                   n_trials: int | None = None,
                   model: str = MODEL,
                   min_exposed: float | None = None,
                   probe: bool | None = None,
                   generation_width: int | None = None) -> None:
    from src.core.loop.campaign import CampaignRecord
    from src.core.loop.llm import LLMAgent

    d = OUT / name
    if (d / "campaign.json").exists():
        sys.exit(f"{d / 'campaign.json'} already exists. A pre-registration is "
                 "written once.")

    # **Everything local is checked before anything remote.** The preflight
    # below is a network round trip to the model; a seed that cannot match the
    # profile, or an unknown profile, is refused for free ahead of it.
    profile = cfg()["profiles"][profile_name]
    seed_text = None
    if seed is not None:
        from src.core.loop.campaign import declares_fit
        seed_text = seed.read_text()
        fitted_profile = bool(profile.get("fit_sessions", 0))
        if declares_fit(seed_text) != fitted_profile:
            sys.exit(
                f"{seed} {'defines' if declares_fit(seed_text) else 'does not define'} "
                f"fit(), but {profile_name} "
                f"{'has' if fitted_profile else 'has no'} fit window. A seed that "
                "does not match the profile is refused at registration rather "
                "than crashing trial 0.")
        # **The seed has to pass the agent's own static check.** The agent edits
        # this file and every edit inherits whatever is already in it; a seed
        # that fails E011 or mypy fails on every trial, the repair loop cannot
        # fix what it did not write, and the budget is spent on
        # `static_check_unrepaired`. nq-ml-01 lost 13 of 20 trials to a `len()`
        # call at module level in the seed. The smoke did not catch it because
        # the smoke bypasses the agent.
        from src.core.loop.llm import LLMAgent
        ok, why = LLMAgent._static_ok(LLMAgent.__new__(LLMAgent), seed_text)
        if not ok:
            sys.exit(
                f"{seed} fails the agent's static check, so every trial that "
                f"edits it would too:\n  {why[:600]}\nFix the seed and register "
                "again.")

    # **An assertion, not an override, and checked before the preflight.** It
    # used to be `cc = {**cc, "min_exposed_fraction": min_exposed}`, which wrote
    # the flag into the record while `run.py` went on reading run.yaml -- so the
    # record claimed a floor nothing applied. The flag now says "I have already
    # set this in run.yaml and I mean it", and disagreeing is a refusal rather
    # than a silent divergence. It costs nothing remote, so it belongs above.
    if min_exposed is not None:
        if not (0.0 < min_exposed <= 1.0):
            sys.exit(f"--min-exposed must be in (0, 1], got {min_exposed}")
        configured = campaign_cfg().get("min_exposed_fraction")
        if configured != min_exposed:
            sys.exit(
                f"--min-exposed {min_exposed} disagrees with "
                f"campaign.min_exposed_fraction {configured!r} in "
                f"config/run.yaml.\n"
                f"run.py reads the floor from run.yaml and never opens "
                f"campaign.json, so registering this way would record a floor "
                f"that nothing enforces. Edit config/run.yaml to "
                f"{min_exposed}, commit it, and register again.")

    print("preflight ...", flush=True)
    served = LLMAgent(model=model, temperature=0.7,
                        max_tokens=PROPOSER_MAX_TOKENS).preflight()
    print(f"  asked {served['requested']!r}  served {served['served']!r}")

    cc = campaign_cfg()
    # N and the model are per-campaign decisions frozen here, like the q. The
    # config carries defaults; a flag at registration is the decision, and the
    # record is what every later stage reads.
    if n_trials is not None:
        if n_trials < 1:
            sys.exit(f"--n must be positive, got {n_trials}")
        cc = {**cc, "n_trials": n_trials}
    if probe is not None:
        # Amendment 7. A flag at registration, frozen below; never read from
        # the config at run time.
        cc = {**cc, "probe_before_trial": bool(probe)}
    agent_cfg = agent_for(None, hypothesis, profile_name, model).config()
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
    agent_cfg["probe_before_trial"] = bool(cc.get("probe_before_trial"))

    d.mkdir(parents=True, exist_ok=True)
    # **`--generation-width auto`: the conditioning stage decides W here.**
    # Brian asked for a pre-run script that sizes parallelism to the machine.
    # Registration is where that can be honest: `stage_run` still refuses rather
    # than resizes, because by then the number is in the record and re-deriving
    # it would make the search path depend on the load at the moment the run
    # happened to start. Here nothing has been spent yet, so a measured W is a
    # declaration that was computed instead of typed -- and the evidence it was
    # computed from is written beside the record.
    capacity_decision = None
    if generation_width == "auto":
        sys.path.insert(0, str(ROOT / "scripts"))
        import capacity as capacity_mod
        capacity_decision = capacity_mod.decide(profile_name)
        if capacity_decision["verdict"] != "OK":
            sys.exit(f"cannot decide a generation width: "
                     f"{capacity_decision['reason']}")
        generation_width = capacity_decision["width"]
        print(f"  capacity decided generation_width={generation_width} "
              f"({capacity_decision['reason']})", flush=True)
        if capacity_decision.get("unproven_width"):
            print(f"  NOTE: {capacity_decision['unproven_width']}", flush=True)
        if capacity_decision.get("warning"):
            print(f"  capacity WARNING: {capacity_decision['warning']}", flush=True)

    record = CampaignRecord(
        campaign_id=name, profile=profile_name, budget=cc["n_trials"],
        q=cc["q"],
        # Frozen before trial 0, like the budget and the q. A campaign that
        # turns this on partway through because the unfitted trials looked poor
        # is a different experiment from the one registered.
        # Permission AND capability. The config says whether the agent may
        # fit; the profile says whether there is a window to fit in. A campaign
        # with no window records `false` however the config is set, because a
        # fitted proposal there passes this gate and is then refused by the
        # engine -- a wasted trial rather than a useful refusal. The record
        # carries what the campaign actually allowed, not what was asked for.
        allow_fitted_proposals=bool(cc.get("allow_fitted_proposals"))
        and bool(profile.get("fit_sessions", 0)),
        min_exposed_fraction=cc.get("min_exposed_fraction"),
        probe_before_trial=bool(cc.get("probe_before_trial")),
        # Amendment 12, frozen before trial 0 like the budget. A campaign that
        # widened its generations halfway through would be two search procedures
        # reported as one.
        generation_width=generation_width,
        # **From the profile, not hard-coded.** These two read `"nq-trades"`
        # and `"train"` literally, which was true of every campaign until one
        # was not: an SPX options campaign would have recorded that it ran on
        # NQ trades. The record is the thing a reader trusts about what a
        # result is a result OF, so it says what the profile actually names.
        agent="src.core.loop.llm.LLMAgent",
        dataset=profile["dataset"], split=profile["split"],
        hypothesis_path=str(hypothesis.relative_to(ROOT)),
        hypothesis_digest=agent_cfg["hypothesis"]["digest"],
        agent_config=agent_cfg,
        note=_campaign_note(profile),
        written_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    # **Seeding is optional, and both branches are the design.** With --seed,
    # trial 0's agent starts from a human-written strategy and edits it. Without,
    # champion.py is absent and the agent writes from scratch -- which must keep
    # working, because a proposer that can only edit is not a proposer. The
    # record says which happened.
    if seed_text is not None:
        (d / "champion.py").write_text(seed_text)
        record.seed_path = str(seed.relative_to(ROOT))
        record.seed_digest = hashlib.sha256(seed_text.encode()).hexdigest()[:16]
        print(f"  seeded champion from {record.seed_path}  ({record.seed_digest})")
    digest = record.write(d / "campaign.json")
    if capacity_decision is not None:
        # Beside `campaign.json`, never inside it: the record's digest is
        # re-verified by every later stage, and a machine measurement belongs in
        # the provenance rather than in the thing being fingerprinted.
        (d / "capacity-at-registration.json").write_text(
            json.dumps(capacity_decision, indent=2, sort_keys=True) + "\n")
    print(f"  registered {name}  N={cc['n_trials']}  q={cc['q']}  "
          f"null_source={cc['null_source']}  correction={cc['correction']}")
    print(f"  digest {digest}")


def stage_run(name: str, ignore_capacity: str | None = None) -> None:
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

    width = max(1, int(record.generation_width or 1))
    # **The conditioning stage (Brian, 2026-10-11).** Ask the machine what it
    # can carry BEFORE building anything, rather than letting it find out at
    # trial 300 of 400. The full test suite was killed by memory pressure on
    # this machine the same day; a campaign is the same accident with three
    # days of wall clock attached.
    #
    # It REFUSES rather than lowering W, and that is the whole design decision.
    # `generation_width` is pre-registered and stamped on every ledger row
    # because it decides what each agent SEES -- `shown` is snapshotted once per
    # generation below, so at W=4 trials 0-3 propose blind and 4-7 see 0-3,
    # while at W=8 all eight propose blind. Sizing W from whatever hardware is
    # to hand would make the search path a property of the laptop, and a
    # quietly narrowed run is a different experiment wearing the same campaign
    # id. W = 1 can never be refused: `max_width` floors at 1.
    sys.path.insert(0, str(ROOT / "scripts"))
    import capacity as capacity_mod
    cap = capacity_mod.plan(width, record.profile)
    (d / "capacity.json").write_text(
        json.dumps({**cap, "ignored": ignore_capacity}, indent=2, sort_keys=True) + "\n")
    print(f"  capacity: W={width} {cap['verdict']}"
          + (f"; machine carries W<={cap['max_width']}, bound by {cap['binding']}"
             if cap.get("binding") else f"; {cap['reason']}"), flush=True)
    if cap.get("warning"):
        print(f"  capacity WARNING: {cap['warning']}", flush=True)
    if cap["verdict"] != "OK" and not ignore_capacity:
        capacity_mod.render(cap)
        sys.exit(
            f"refusing to start {name} at W={width}: {cap['reason']}.\n"
            f"W is NOT being lowered for you -- generation_width is "
            f"pre-registered and changes what each agent sees, so a narrower "
            f"run is a different experiment. Either free capacity, re-register "
            f"at W<={cap.get('max_width', 1)}, or pass --ignore-capacity "
            f"'<reason>' to proceed on the record.")
    if ignore_capacity and cap["verdict"] != "OK":
        print(f"  capacity OVERRIDDEN: {ignore_capacity}", flush=True)



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

    # From the record, not from PROFILE: a fitted campaign registered against
    # `nq-train-fitted` and its agent has to be told the fit window and the
    # scored count, which `run_facts` reads off that profile.
    # Model from the record too. Registering one model and running another
    # would make agent_config describe a campaign that never happened.
    model = (record.agent_config or {}).get("model", MODEL)
    agent = agent_for(champion if champion.exists() else None, hyp_path,
                      record.profile, model)
    def _arm_probe(a, root: pathlib.Path | None = None) -> None:
        """Amendment 7 on one agent, from the record.

        The probe writes `purpose: check` rows to the campaign's own probe
        ledger, beside the trial ledger and never in it, so `global_n()` cannot
        pick them up and a reader can see every synthetic run that happened.

        **A function because Amendment 12 made more than one agent.** This used
        to be inline on `agent` alone, and the generational block then built
        `width - 1` more with `agent_for(...)`, which arms nothing -- so in a
        width-4 campaign 3 of every 4 proposals skipped the probe entirely.
        Measured on nq-s06-01: 41 probe rows for 100 trials. nq-s06-02 then
        crashed 33 of 80 proposals on two API-misuse shapes the probe catches in
        one synthetic run, because three quarters of them never got the chance
        to repair.
        """
        if not record.probe_before_trial:
            return
        import functools
        from src.core.loop.probe import probe_source
        a.probe_before_trial = True
        # **`strategy_path` per worker, and without it the probe raced.**
        # `probe_source` defaults to the REPOSITORY's
        # `src/strategies/current.py`, and this passed no path -- so under
        # Amendment 12 all `width` agents probed through one file. Agent A wrote
        # its source, B overwrote it, and A's `run.py` then loaded B's strategy:
        # a probe result attributed to the wrong candidate, which is the exact
        # fault worker isolation exists to prevent. It also left the repository's
        # file dirty at the end of session 07, so the test suite could not even
        # collect until someone restored it.
        #
        # The scored TRIALS were never affected -- `run_trial` takes `root` and
        # writes the worker's file -- so session 07's results stand. Only the
        # probe leaked, and only its attribution.
        a.probe = functools.partial(
            probe_source, ledger=d / "probe-ledger.jsonl",
            strategy_path=(root or ROOT) / "src" / "strategies" / "current.py")

    _arm_probe(agent)
    if record.probe_before_trial:
        print("  probe before trial: ON (Amendment 7); synthetic rows in "
              "probe-ledger.jsonl", flush=True)
    # **Amendment 12: generations.** W trials share one history and run at the
    # same time; the next generation sees all of them. W = 1 is the chain the
    # first eighteen campaigns ran, and it keeps running in THIS repository
    # rather than in a worker, so nothing about a W = 1 campaign changes.
    workers = []
    if width > 1:
        sys.path.insert(0, str(ROOT / "scripts"))
        import worker_tree
        print(f"  Amendment 12: generations of {width}; building working copies",
              flush=True)
        workers = worker_tree.create(width, quiet=True)
        if worker_tree.verify(width):
            sys.exit("worker isolation failed; refusing to run trials that would "
                     "overwrite each other's strategy")
        # The agents are per-slot: `propose` is a model call and an LLMAgent
        # carries its own drain log, so W of them is W bills, not one shared and
        # raced.
        # Re-armed on its own worker: `_arm_probe(agent)` above pointed it at
        # the repository, which is right at width 1 and a race above it.
        _arm_probe(agent, workers[0])
        agents = [agent]
        for slot in range(1, width):
            extra = agent_for(champion if champion.exists() else None,
                              hyp_path, record.profile, model)
            # Every agent, on its own worker. See `_arm_probe`.
            _arm_probe(extra, workers[slot])
            agents.append(extra)
    else:
        agents = [agent]

    def _merge(worker: pathlib.Path, wledger: pathlib.Path) -> None:
        """Copy everything a worker stored beside its ledger into the campaign's.

        `run.py` writes a trial's artefacts next to the ledger it was given, so
        for a parallel trial they land in the worker. The row names a digest and
        a reader follows it from the CAMPAIGN directory, so anything left behind
        is a row pointing at nothing.

        **Every sibling directory, not a list of names.** The first version
        copied `sources/` only, and `runlog.py` writes three --
        `sources/`, `weights/` and `fills/`. The missing one was `fills/`, which
        is what the sign-flip null reconstructs round turns from, so
        `stage_nulls` found no fill log for any candidate, wrote 100 empty
        samples, and the gate reported `n_nulls = 0` and `p = 1.0` on a campaign
        with 45 finite scores. It cost nq-s05b-01 its nulls and they had to be
        rescued out of the workers before the next campaign recreated them.
        Enumerating names is what broke it; this copies whatever is there.
        """
        if not wledger.parent.is_dir():
            return
        for src_dir in sorted(wledger.parent.iterdir()):
            if not src_dir.is_dir():
                continue
            dest = ledger.parent / src_dir.name
            dest.mkdir(parents=True, exist_ok=True)
            for f in src_dir.iterdir():
                if not f.is_file():
                    continue
                target = dest / f.name
                if not target.exists():
                    target.write_bytes(f.read_bytes())

    # **Amendment 13: a rehearsal's trials are `purpose: check`.** Derived from
    # the profile in the committed record, not from a flag -- the `rehearsal`
    # profile is synthetic, and a run against synthetic data is not a look at
    # our market data. Tying it to the record means no caller can relabel a real
    # campaign's trials, which is the property run.py's own `--purpose` rule
    # exists to protect.
    purpose = "check" if record.profile == "rehearsal" else "trial"
    if purpose == "check":
        print("  REHEARSAL: profile 'rehearsal' is synthetic, so these trials "
              "are purpose=check and count toward no N (Amendment 13)",
              flush=True)

    t0 = time.monotonic()
    for gen_start in range(start, record.budget, width):
        gen = list(range(gen_start, min(gen_start + width, record.budget)))
        # **One history for the whole generation, snapshotted before any of it
        # runs.** This is the sentence Amendment 12 amended: everyone here sees
        # every trial completed before the generation began and nothing from its
        # own.
        shown = list(rows)
        champ_now = champion if champion.exists() else None

        def _one(slot_and_index):
            slot, i = slot_and_index
            a = agents[slot]
            a.champion = champ_now
            source = a.propose(shown)
            if width == 1:
                return i, slot, source, run_trial(
                    source, profile=record.profile, ledger=ledger,
                    campaign_id=record.campaign_id, index=i,
                    allow_fitted=record.allow_fitted_proposals,
                    purpose=purpose)
            w = workers[slot]
            wledger = w / "runs" / "ledger.jsonl"
            row = run_trial(source, profile=record.profile, ledger=wledger,
                            campaign_id=record.campaign_id, index=i,
                            allow_fitted=record.allow_fitted_proposals,
                            root=w, purpose=purpose)
            _merge(w, wledger)
            return i, slot, source, row

        pairs = list(enumerate(gen))
        if width == 1:
            results = [_one(pairs[0])]
        else:
            with cf.ThreadPoolExecutor(max_workers=len(pairs)) as pool:
                results = list(pool.map(_one, pairs))

        # **Merged in index order, always.** The champion advances on a strict
        # improvement, so the order results are folded in decides which of two
        # equal-scoring candidates becomes the base. Thread completion order is
        # arbitrary; trial index is not.
        for i, slot, source, row in sorted(results, key=lambda r: r[0]):
          # **Stamped BEFORE the row is written, not after.** Amendment 12 says
          # the generation index is on every ledger row, and the first version
          # of this loop set it on the in-memory copy after appending -- so the
          # file recorded `null` for all eight trials of the proof run and the
          # amendment's own disclosure requirement was unmet by the code that
          # implements it.
          row["generation"] = gen_start // width if width > 1 else None
          row["generation_width"] = width
          if width > 1:
            # **Repoint the row's artefact paths at the campaign.** `run.py`
            # records them relative to the root it ran under, so a parallel
            # trial's row says `runs/fills/<digest>.jsonl.gz` -- which resolves
            # against THIS repository and finds nothing, because the file was
            # merged into `runs/campaigns/<name>/fills/`. A sequential campaign
            # records the campaign-relative path, so this restores the only
            # shape every reader already understands.
            #
            # It is why `stage_nulls` found no fill log for any of nq-s05b-01's
            # candidates even after the files were merged: the files were there
            # and the rows pointed somewhere else.
            rel = ledger.parent.relative_to(ROOT).as_posix()
            for key in [k for k in row if k.endswith("_source")]:
                v = row.get(key)
                if isinstance(v, str) and v.startswith("runs/") \
                        and not v.startswith("runs/campaigns/"):
                    row[key] = f"{rel}/{v[len('runs/'):]}"
          if width > 1:
            # The canonical ledger is this campaign's. A worker's copy is scratch
            # and is not on any path `global_n()` reads, so a row counts once.
            with ledger.open("a") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
          agent = agents[slot]
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
                f"{el / 60:.0f}m elapsed, eta "
                f"{(el / done) * (record.budget - i - 1) / 60:.0f}m", flush=True)
    if workers:
        print(f"  {len(workers)} worker(s) left in place for inspection; "
              f"scripts/worker_tree.py --destroy removes them", flush=True)
    print(f"{name}: {record.budget} trials in {(time.monotonic() - t0) / 60:.0f} min "
          f"(generations of {width})")


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
    # **The campaign's OWN profile, not the module default.** This read
    # `cfg()["profiles"][PROFILE]`, so the null was always reassembled onto the
    # nq-train calendar whatever the campaign actually ran on. Every real
    # campaign to date ran on nq-train or nq-train-fitted, which share a dataset
    # and split, so it was invisible -- a campaign on any other profile got a
    # calendar its round turns do not appear in, and `null_sample` returned ZERO
    # draws for every candidate. Found 2026-10-05 by the first synthetic session
    # rehearsal: the log said "384 scored sessions" for a campaign that scored
    # 105.
    from src.core.loop.campaign import CampaignRecord as _CR
    _rec, _ = _CR.read(d / "campaign.json")
    profile = cfg()["profiles"][_rec.profile]
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


def _record_outcome(name: str) -> None:
    """The feedback edge: write this campaign's verdicts back onto the paper that
    suggested its question, if a paper did.

    **Automatic, and that placement is the point.** Stage 0b was built as a
    standalone command, nobody triggered it, and the corpus silently went stale
    until someone asked why a paper had no body. A feedback edge that has to be
    remembered is one that will not happen. Best-effort: a campaign's report is
    worth having even if the registry write fails, so this never raises.

    `scripts/record_outcome.py` is the same code as a command, for backfill.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "record_outcome", pathlib.Path(__file__).with_name("record_outcome.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        import json as _json
        reg = _json.loads(mod.REGISTRY.read_text())
        v = mod.verdicts_for(name)
        if v is None:
            return
        pid = mod.paper_for(reg, v["hypothesis"])
        if pid is None:
            print("  feedback edge: no paper claims this hypothesis, so there is "
                  "nothing to record against (human-written question)")
            return
        row = reg["papers"][pid]
        ids = list(row.get("campaign_ids") or [])
        if name not in ids:
            ids.append(name)
        outs = [o for o in (row.get("outcome") or [])
                if o.get("campaign_id") != name] + [v]
        row["campaign_ids"] = ids
        row["outcome"] = sorted(outs, key=lambda o: o["campaign_id"])
        mod.REGISTRY.write_text(_json.dumps(reg, indent=2, sort_keys=True) + "\n")
        # **Keys that exist.** This read a `conclusion` key, which
        # `verdicts_for()` has never returned, and the KeyError was raised AFTER
        # the registry write -- so the edge did its job and then reported
        # "feedback edge FAILED", which is the opposite of what happened. A
        # best-effort step that lies about succeeding is worse than one that
        # stays quiet.
        print(f"  feedback edge: recorded on paper {pid} -- gate {v['gate']}, "
              f"committee {v['committee']}"
              + (f" (vetoed by {', '.join(v['vetoed_by'])})"
                 if v.get("vetoed_by") else ""))
    except Exception as exc:                                      # noqa: BLE001
        print(f"  feedback edge FAILED (report stands): {type(exc).__name__}: {exc}")


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
    # **Retried on a fault that is the endpoint's.** On 2026-09-30 a single
    # `ReadTimeout` recorded this campaign's committee as ERROR, which is a
    # correct refusal -- "no verdict" is not "no veto" -- but it is a bad
    # outcome to accept from a network blip when the whole campaign has already
    # been paid for. Four auditors unread because of a 60-second timeout is
    # throwing away the most expensive part of the run.
    _TRANSIENT = ("ReadTimeout", "ConnectTimeout", "Timeout", "ConnectionError",
                  "RemoteDisconnected", "ServiceUnavailable", "502", "503",
                  "504")
    _BACKOFF = (30, 120, 300)
    cv = exc = None
    for _attempt in range(len(_BACKOFF) + 1):
        try:
            from audit.committee.review import review
            # **The committee picks its own model; the campaign only says
            # what wrote the code.** Passing the proposer's model here is the
            # independence defect docs/setup/MODEL-ACCESS.md recorded: all 17
            # verdicts to 2026-10-04 had a model auditing its own work.
            proposer = (record.agent_config or {}).get("model", MODEL)
            cv = review(tier2, source=best_source, hypothesis=hyp_text,
                        proposer_model=proposer)
            exc = None
            break
        except Exception as e:                                    # noqa: BLE001
            exc = e
            blip = any(t in f"{type(e).__name__}: {e}" for t in _TRANSIENT)
            if not blip or _attempt == len(_BACKOFF):
                break
            wait = _BACKOFF[_attempt]
            print(f"    {type(e).__name__} is the endpoint's fault. Waiting "
                  f"{wait}s, then asking the committee again.", flush=True)
            import time as _t
            _t.sleep(wait)
    if exc is not None:
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
        _record_outcome(name)
        return
    (d / "committee-verdict.json").write_text(
        json.dumps(cv.as_dict(), indent=2, default=str))
    print(f"  committee: {cv.verdict}"
          + (f"  vetoed by {', '.join(v.axis for v in cv.auditors if v.verdict == 'VETO')}"
             if cv.verdict == "VETO" else ""))
    for v in cv.auditors:
        print(f"    {v.axis:<12} {v.verdict}")
    _record_outcome(name)


def _width_arg(v: str):
    """`--generation-width N` or `--generation-width auto`.

    `auto` is resolved at REGISTRATION, where choosing is legitimate: no trial
    has run and no score exists, so a computed W is still a declaration frozen
    with the rest of the pre-registration. It is not accepted on later stages --
    they read the number the record already fixed.
    """
    if v == "auto":
        return "auto"
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is neither an integer nor 'auto'")
    if n < 1:
        raise argparse.ArgumentTypeError("generation width is at least 1")
    return n


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
    ap.add_argument("--n", type=int, default=None,
                    help="trials in this campaign; --stage register only, frozen "
                         "into campaign.json. Default is campaign.n_trials in "
                         "config/run.yaml.")
    ap.add_argument("--model", default=MODEL,
                    help=f"the proposing model; --stage register only, frozen "
                         f"into campaign.json and read back by run and report. "
                         f"Default {MODEL}.")
    ap.add_argument("--min-exposed", type=float, default=None,
                    help="Amendment 6 exposure floor for this campaign, in (0, 1]; "
                         "--stage register only. It must AGREE with "
                         "campaign.min_exposed_fraction in config/run.yaml, "
                         "which is what run.py actually scores from; "
                         "disagreeing is refused rather than overridden.")
    ap.add_argument("--ignore-capacity", default=None, metavar="REASON",
                    help="proceed even though the machine cannot carry the "
                         "declared generation_width; the reason is recorded in "
                         "the campaign's capacity.json")
    ap.add_argument("--generation-width", type=_width_arg, default=None,
                    help="Amendment 12: how many trials share one history and "
                         "run at the same time; --stage register only, frozen "
                         "into campaign.json. Omit or 1 for the chain every "
                         "campaign before 2026-10-04 ran. Above 1 the trials of "
                         "a generation are proposed from the same history and "
                         "scored concurrently in isolated working copies. It "
                         "buys wall clock and costs power -- correlated "
                         "candidates mean fewer effective looks than N, so "
                         "max_of_n over-corrects. Read Amendment 12.")
    ap.add_argument("--probe", action=argparse.BooleanOptionalAction,
                    default=None,
                    help="--stage register only. Amendment 7: run each proposal "
                         "once on a synthetic profile before its trial and let "
                         "the agent repair a crash or a no-op edit, bounded. "
                         "Frozen into campaign.json; later stages read it there. "
                         "Omit to take campaign.probe_before_trial from "
                         "config/run.yaml; --no-probe turns it off for one "
                         "campaign. It was `store_true, default=False` until "
                         "Amendment 7 was signed, which meant the flag silently "
                         "overrode the config to False on every registration and "
                         "the config default could never take effect.")
    ap.add_argument("--seed", type=pathlib.Path, default=None,
                    help="a strategy file to start trial 0 from; --stage register "
                         "only. Copied to champion.py and recorded by digest. "
                         "Omit and the agent writes from scratch, which must keep "
                         "working.")
    ap.add_argument("--profile", default=PROFILE,
                    help=f"run profile; --stage register only, and it is written "
                         f"into campaign.json. Default {PROFILE}. Use "
                         f"nq-train-fitted for a campaign whose strategies fit, "
                         f"which withholds a fit window from the score. Later "
                         f"stages read it from the record and ignore this flag.")
    a = ap.parse_args()
    if a.stage == "register":
        hyp = a.hypothesis.resolve()
        try:
            hyp.relative_to(ROOT)
        except ValueError:
            sys.exit(f"{hyp} is outside the repository. A campaign's question has "
                     "to be a committed file, or the record points at nothing a "
                     "reader can check.")
        profiles = cfg()["profiles"]
        if a.profile not in profiles:
            sys.exit(f"unknown profile {a.profile!r}. config/run.yaml has: "
                     f"{', '.join(sorted(profiles))}")
        seed = None
        if a.seed is not None:
            seed = a.seed.resolve()
            if not seed.exists():
                sys.exit(f"--seed {a.seed} does not exist")
            try:
                seed.relative_to(ROOT)
            except ValueError:
                sys.exit(f"{seed} is outside the repository; a seed has to be a "
                         "committed file or the record points at nothing.")
        stage_register(a.name, hyp, a.profile, seed, a.n, a.model, a.min_exposed,
                       a.probe, a.generation_width)
    else:
        # Refused rather than ignored. Silently accepting it on a run stage is how
        # someone believes they changed the question and did not.
        if a.hypothesis.resolve() != DEFAULT_HYPOTHESIS.resolve():
            sys.exit(f"--hypothesis applies to --stage register only; "
                     f"--stage {a.stage} reads it from campaign.json.")
        if a.profile != PROFILE:
            sys.exit(f"--profile applies to --stage register only; "
                     f"--stage {a.stage} reads it from campaign.json.")
        if a.seed is not None:
            sys.exit(f"--seed applies to --stage register only; "
                     f"--stage {a.stage} reads champion.py from the campaign.")
        if a.n is not None or a.model != MODEL or a.min_exposed is not None \
                or a.probe:
            sys.exit(f"--n, --model, --min-exposed and --probe apply to --stage "
                     f"register only; --stage {a.stage} reads them from campaign.json.")
        if a.generation_width == "auto":
            sys.exit("--generation-width auto applies to --stage register only. "
                     "A later stage reads the width the record already fixed; "
                     "re-deciding it would change what each agent sees "
                     "mid-campaign.")
        if a.ignore_capacity and a.stage != "run":
            sys.exit(f"--ignore-capacity applies to --stage run only; "
                     f"--stage {a.stage} builds no workers.")
        # After the argument refusals: those are about this invocation and are
        # free, this one reads the record off disk.
        _refuse_if_floor_drifted(a.name)
        if a.stage == "run":
            stage_run(a.name, ignore_capacity=a.ignore_capacity)
        else:
            {"nulls": stage_nulls, "validate": stage_validate,
             "report": stage_report}[a.stage](a.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
