#!/usr/bin/env python3
"""One research session: K campaigns, then validation at the stop, then L2.

    python scripts/research_session.py --plan session-01.yaml --dry-run
    python scripts/research_session.py --plan session-01.yaml
    python scripts/research_session.py --plan session-01.yaml --resume

A session is what an overnight run means: several campaigns under different
questions, and at the end the accounting that turns them into one claim or, far
more often, into nothing.

## Why validation happens at the stop and not per campaign

§4 is explicit and our practice has not followed it:

> Every campaign's winner is stored (sources are content-addressed). At the stop,
> all M winners are scored on validation *then*, at once. This removes the
> temptation rather than relying on discipline, which is this project's usual
> move -- a human who reads a committee report that contains a validation number
> has seen it, whatever they intended.

`nq_campaign.py --stage validate` scores one campaign's winner immediately, and
that is how every campaign so far was run. This driver does it the way §4 says:
**it runs register, run, nulls and report for every campaign first, and only then
validates, all of them, in one pass.** The point is not tidiness. §4's defence
against optional stopping is that a human deciding whether to run campaign k+1
has not seen any validation number, and that defence only exists if the numbers
do not exist yet.

A campaign whose report ran without validation gets a committee that never saw a
validation number either, which is a real difference in what the auditors judge.
That is the intended consequence, not a side effect.

## Ctrl-C is the stop signal, and that is a protocol decision not a convenience

**Section 4 lets a human stop for any reason that is not a validation result.**
An unattended session has no fixed right length, and a plan written in advance
has to guess one. Ctrl-C is the honest version: you stop when you decide to stop,
and *because no validation number exists yet*, you provably cannot be stopping
because of one. That is a better realisation of section 4 than a fixed K.

So the first Ctrl-C means **"stop proposing, go to the stop"**: no new campaign
starts, the session moves straight to phase 2 and validates every champion it
has, then phase 3, then phase 4. A **second** Ctrl-C during those phases aborts
outright -- otherwise there is no way out of a wedged validation run.

A campaign interrupted mid-flight enters the record but **not the L2 family**:
the family is the campaigns that reached `report` and therefore have a winner.
The interrupted one is named in the session record as having run and produced no
winner, so its trials are visible in the count even though it contributes no
candidate -- the same treatment `max_of_n` gives a trial whose null is empty.

## What it will not do

**It will not take the holdout look unless you armed it before the session
started.** By default phase 4 prints what `scripts/holdout.py --check` would say
and stops, because §8 gives 73 sessions one look **ever** -- not one per session --
and a driver that spends it as the last line of every overnight script will spend
it by accident on the first one.

`--holdout-if-l2-passes "<reason>"` arms it. The flag is deliberately only
available **at launch**, which is the protocol-correct moment to decide: at that
point no validation number exists, so the decision to spend the look cannot have
been made in response to one. `holdout.py` still applies every one of its own
refusals, including that `runs/holdout-result.json` existing is itself a refusal,
so arming it in session two after spending it in session one changes nothing.

**It will not choose the family.** The L2 family is every campaign in the plan,
written into the plan before the session runs, which is the same rule
`l2_gate.py` enforces by making `--campaigns` explicit: dropping the campaigns
that did badly shrinks M and inflates the maximum's significance.

**It will not invent hypotheses.** Each campaign in the plan names a committed
hypothesis file. Amendment 8 makes the commit the human act, so a session cannot
distil and register in one breath.

## Two shapes of plan: a list, or a family

**A list** names K campaigns written in advance. M is the list.

**A family** names one seed question and a number of iterations, and campaign
k+1 runs on a question refined from campaign k's own outcome. That is the L1.5
§4 anticipated, and it is what an overnight session is actually for: run, read
what the auditors objected to, sharpen the question, run again.

```yaml
session: session-02
family:
  seed: config/hypotheses/intraday-continuation.md
  iterations: 5
  n: 20
  model: deepseek-v4-pro
  prefix: nq-s02
```

**M is declared even though its members are not.** The count is fixed in the
plan before anything runs -- five iterations, five campaigns in the family --
while *which* questions they ask is decided as it goes. That is the honest
statement of what is and is not pre-registered here, and it is the distinction
§4's correction actually needs: the multiplicity is the number of looks, and the
number of looks is written down first.

**Refinement happens during phase 1, and that is what makes it legal.**
`refine_hypothesis.py` refuses to run if any validation artefact is readable for
the campaign it is given. During phase 1 none exists, because validation runs at
the stop. The ordering enforces §4 rather than a promise to be careful.

Each refined question is committed by the machine under **Amendment 11**, marked
`Machine-Written`, and carries Amendment 11's containment: it may spend trials
and may not support a claim until a human has read it.

## The plan file

```yaml
session: session-01
campaigns:
  - name: nq-mike-01
    hypothesis: config/hypotheses/flow-continuation-5m.md
    profile: nq-train          # optional, defaults to nq-train
    n: 20                      # optional, defaults to the config
    model: claude-haiku-4-5    # optional
    min_exposed: 0.70          # optional
    probe: false               # optional; Amendment 7. Omitted means the
                               # config default. Set false where the probe's
                               # synthetic profile cannot execute the kind of
                               # proposal this campaign asks for -- an options
                               # campaign is the case that exists.
    generation_width: 4        # optional; Amendment 12. Omit or 1 for the
                               # chain. Above 1, trials of a generation share
                               # one history and run at the same time.
                               # `auto` lets scripts/capacity.py measure this
                               # machine and choose, ONCE for the whole plan,
                               # before campaign 1 registers. The number it
                               # picks is written into every member's
                               # pre-registration as an ordinary declaration.
```

Written before the session starts, and copied into the session record. `--resume`
picks up at the first campaign that has no ledger, so a machine that died in hour
six does not cost hours one to five.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os as _os
_RUNS = pathlib.Path(_os.environ.get("PRACTICUM_RUNS_ROOT") or (ROOT / "runs"))
CAMPAIGNS = _RUNS / "campaigns"
SESSIONS = _RUNS / "sessions"
PY = sys.executable


LOGS: pathlib.Path | None = None        # set once the session is named


def _run(argv: list[str], *, label: str) -> tuple[int, str]:
    """One stage. Full output to a file, one line to the console.

    **The whole output is written somewhere, always.** The first version printed
    only the last line, and session-01 died with three campaigns reporting
    `exit 1` and no reason anywhere -- the cause was a transient model
    substitution visible only in a traceback that had been captured and dropped.
    Five minutes lost there; it could as easily have been eight hours. A summary
    that replaces the evidence instead of pointing at it is the same defect this
    project has now hit five times.
    """
    t0 = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, cwd=str(ROOT))
    mins = (time.monotonic() - t0) / 60
    body = (proc.stdout or "") + (proc.stderr or "")
    where = ""
    if LOGS is not None:
        LOGS.mkdir(parents=True, exist_ok=True)
        f = LOGS / f"{label.replace(' ', '-').replace('/', '-')}.log"
        f.write_text(body)
        where = f"  -> {f.relative_to(ROOT)}"
    tail = (proc.stdout or "").strip().splitlines()
    last = tail[-1] if tail else ""
    print(f"    {label:<34} exit {proc.returncode}  {mins:5.1f}m  {last[:60]}"
          + (where if proc.returncode else ""), flush=True)
    if proc.returncode:
        # The last 12 lines inline too, because a reader watching a session
        # should not have to go and open a file to learn what broke.
        for line in body.strip().splitlines()[-12:]:
            print(f"        | {line}"[:150], flush=True)
    return proc.returncode, body


# Faults that are the endpoint's and not the candidate's. `--stage run` resumes
# at the first unfilled trial, so retrying costs nothing already done -- and the
# guards these raise must NOT be softened: `ModelSubstituted` exists because
# continuing would write a record naming a model that did not answer. The right
# response to a transient substitution is to try again, not to believe it.
TRANSIENT = ("ModelSubstituted", "ReadTimeout", "ConnectTimeout", "Timeout",
             "RemoteDisconnected", "ConnectionError", "ServiceUnavailable",
             "502", "503", "504")
# Progressive, because the substitution window is minutes long, not seconds.
# Measured on 2026-09-30: the same 157,505-character prompt was substituted on
# every attempt for several minutes and then served correctly by the model asked
# for, with nothing changed. Three tries 30s apart did not outlast it. A campaign
# is hours; waiting ten minutes for the endpoint is the cheapest thing here.
STAGE_BACKOFF_S = (30, 120, 300, 600, 600)
MAX_STAGE_RETRIES = len(STAGE_BACKOFF_S)
# **The run stage waits longer, because it is the expensive one to lose.**
# 2026-10-05: nq-s06-03 was substituted at trial 36 of 100 and the substitution
# outlasted all five attempts above -- 27 minutes was not enough, where the
# window measured on 2026-09-30 was "several minutes". A run stage is ~2 hours of
# work and resumes where it stopped, so another half hour of waiting is cheap
# against losing it. The other stages are minutes and keep the shorter budget.
#
# This is not the fix that matters, and it is recorded here so nobody thinks it
# is: the fix is that a failed run no longer spends a validation look. Inflating
# a retry budget buys time against an outage, never correctness.
RUN_BACKOFF_S = STAGE_BACKOFF_S + (900, 900)
MAX_RUN_RETRIES = len(RUN_BACKOFF_S)


def _retrying(argv: list[str], *, label: str, retries: int = 0, backoff: tuple | None = None):
    """Run it, and try again on a fault that is the endpoint's rather than ours.

    Used by the campaign stages AND by the refinement. The refinement was left
    out of this on 2026-09-30 and a single `ReadTimeout` on the gateway preflight
    ended a five-campaign session after one campaign.
    """
    schedule = backoff or STAGE_BACKOFF_S
    code, body = 1, ""
    for attempt in range(retries + 1):
        code, body = _run(argv, label=label + (f" (retry {attempt})"
                                               if attempt else ""))
        if code == 0 or attempt == retries:
            return code, body
        hit = next((t for t in TRANSIENT if t in body), None)
        if hit is None:
            return code, body
        wait = schedule[min(attempt, len(schedule) - 1)]
        print(f"      {hit} is the endpoint's fault, not ours. Waiting {wait}s, "
              f"then retrying.", flush=True)
        time.sleep(wait)
    return code, body


def campaign_stage(name: str, stage: str, extra: list[str] | None = None,
                   *, retries: int = 0, backoff: tuple | None = None):
    return _retrying([PY, "scripts/nq_campaign.py", "--stage", stage,
                      "--name", name, *(extra or [])],
                     label=f"{name} {stage}", retries=retries, backoff=backoff)


def trials_run(name: str) -> int:
    f = CAMPAIGNS / name / "ledger.jsonl"
    if not f.is_file():
        return 0
    return len([x for x in f.read_text().splitlines() if x.strip()])


def budget_of(name: str) -> int | None:
    f = CAMPAIGNS / name / "campaign.json"
    if not f.is_file():
        return None
    return (json.loads(f.read_text()).get("record") or {}).get("budget")


def already_run(name: str) -> bool:
    """Did this campaign FINISH its pre-registered budget?

    **This was `ledger.jsonl exists`, and that lost a campaign.** nq-s06-03 died
    at trial 36 of 100 on a gateway substitution; it has a ledger, so a
    `--resume` would have called it "already run" and skipped it forever. The
    campaign was recoverable the whole time -- `--stage run` resumes at
    `len(rows)` -- and nothing would ever have resumed it.

    A partial campaign now reads as unfinished, so a resume continues it to its
    registered budget. That is the honest reading of a budget: 36 of 100 trials
    is not a campaign that ran.
    """
    budget = budget_of(name)
    if budget is None:
        return (CAMPAIGNS / name / "ledger.jsonl").is_file()
    return trials_run(name) >= budget


def registered(name: str) -> bool:
    return (CAMPAIGNS / name / "campaign.json").exists()


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", required=True, type=pathlib.Path)
    ap.add_argument("--resume", action="store_true",
                    help="skip campaigns that already have a ledger")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and what would run; touch nothing")
    ap.add_argument("--train-only", action="store_true",
                    help="stop after phase 1. No validation, no L2, no holdout. "
                         "For a measurement ABOUT the machinery rather than a "
                         "search for a candidate: validation is one look per "
                         "campaign and spending it to audit a process is the "
                         "same mistake as opening the holdout to test plumbing.")
    ap.add_argument("--holdout-if-l2-passes", metavar="REASON", default=None,
                    help="arm the single look, BEFORE the session starts and so "
                         "before any validation number exists. Phase 4 then runs "
                         "scripts/holdout.py with this reason, but only if L2 "
                         "PASSED and holdout.py's own refusals all clear. "
                         "Omit it and phase 4 only checks.")
    a = ap.parse_args()

    import yaml
    if not a.plan.exists():
        sys.exit(f"no plan at {a.plan}")
    plan = yaml.safe_load(a.plan.read_text())
    session = str(plan.get("session") or a.plan.stem)
    entries = plan.get("campaigns") or []
    fam = plan.get("family")
    if fam and entries:
        sys.exit("a plan is a list of campaigns OR a family, not both: a family "
                 "decides its own members, so a list beside it is ambiguous.")
    if fam:
        iterations = int(fam.get("iterations") or 0)
        if iterations < 2:
            sys.exit("a family needs at least 2 iterations; one is just a "
                     "campaign, and the list form says that more plainly.")
        prefix = str(fam.get("prefix") or session)
        seed = ROOT / str(fam["seed"])
        if not seed.is_file():
            sys.exit(f"no seed hypothesis at {fam['seed']}")
        # **`refine: false` is the control arm.** Every campaign runs the SAME
        # question, so the family measures the run-to-run spread of best-of-N
        # under a fixed question and a fixed proposer -- the number without which
        # no sequence of per-campaign p values can be read at all (Task 72).
        refines = fam.get("refine", True)
        entries = [{"name": f"{prefix}-{k + 1:02d}",
                    "hypothesis": str(fam["seed"]) if (k == 0 or not refines)
                                  else None,
                    "profile": fam.get("profile"), "n": fam.get("n"),
                    "model": fam.get("model"),
                    "min_exposed": fam.get("min_exposed"),
                    # **Amendment 12, and the reason this line exists on its
                    # own.** The flag was added to the argv loop below without
                    # being added here, so `generation_width: 4` in a plan
                    # reached nothing and session-05 registered its first
                    # campaign as a chain -- the 19-hour path the amendment was
                    # signed to avoid. A flag the driver can pass and the plan
                    # cannot set is a flag that silently does nothing.
                    "generation_width": fam.get("generation_width"),
                    "probe": fam.get("probe")}
                   for k in range(iterations)]
    if not entries:
        sys.exit("the plan names no campaigns")

    # **`generation_width: auto` is decided ONCE, for the whole plan.**
    # Brian's conditioning stage, at the only granularity that is coherent here:
    # the L2 gate corrects over a declared family, and W decides what each agent
    # sees, so two members of one family running at different widths would be
    # two different search procedures inside one multiplicity correction. Per
    # campaign would also re-measure a loaded laptop mid-session and drift
    # downward as the night went on.
    #
    # Decided before campaign 1 registers, which is still before anything is
    # spent, and then written into every member's pre-registration as an
    # ordinary declared integer.
    widths = {c.get("generation_width") for c in entries}
    if "auto" in widths:
        if widths - {"auto"}:
            sys.exit("a plan sets generation_width: auto on some campaigns and "
                     "a number on others. W decides what each agent sees, so "
                     "one family runs at one width: use auto for all or none.")
        sys.path.insert(0, str(ROOT / "scripts"))
        import capacity as capacity_mod
        # The default comes from `nq_campaign` rather than being retyped here:
        # a plan that omits `profile` gets whatever that script's default is,
        # and two copies of that string would be one rename from disagreeing
        # about which profile was measured.
        import nq_campaign as campaign_mod
        prof = next((c.get("profile") for c in entries if c.get("profile")), None)
        decision = capacity_mod.decide(prof or campaign_mod.PROFILE)
        if decision["verdict"] != "OK":
            sys.exit(f"cannot decide a generation width for this session: "
                     f"{decision['reason']}")
        for c in entries:
            c["generation_width"] = decision["width"]
        print(f"capacity decided generation_width={decision['width']} for all "
              f"{len(entries)} campaign(s) ({decision['reason']})", flush=True)
        if decision.get("unproven_width"):
            print(f"  NOTE: {decision['unproven_width']}", flush=True)
        if decision.get("warning"):
            print(f"  capacity WARNING: {decision['warning']}", flush=True)

    names = [str(c["name"]) for c in entries]
    if len(set(names)) != len(names):
        sys.exit(f"duplicate campaign names in the plan: {names}")

    # **Its own process group, so the cue reaches the campaign as well as the
    # driver.** An unattended session is started detached, where there is no
    # terminal to press Ctrl-C into, so the cue arrives as a signal instead. Sent
    # to the driver's pid alone it raises KeyboardInterrupt here while the child
    # `run.py` carries on orphaned; sent to the GROUP it stops both. Becoming the
    # group leader is what makes `kill -INT -<pid>` mean what it should. macOS has
    # no `setsid(1)`, which is why this is in the program rather than the launch
    # line.
    try:
        os.setpgrp()
    except OSError:
        pass        # already a leader, or a platform without process groups

    print(f"session {session}: {len(entries)} campaign(s)"
          + ("" if not fam else
             f" -- a FAMILY refined from {fam['seed']}"
             if fam.get("refine", True) else
             f" -- a CONTROL family, all on {fam['seed']}"))
    if fam and not fam.get("refine", True):
        print(f"  CONTROL ARM: all {len(entries)} campaigns run the SAME "
              f"question.\n  This measures the run-to-run spread of best-of-N, "
              f"which is what makes a\n  sequence of per-campaign results "
              f"readable at all.")
    elif fam:
        print(f"  M = {len(entries)} is declared; WHICH questions they ask is "
              f"decided as it goes.\n  Each refinement is machine-committed "
              f"under Amendment 11 and may not support a claim.")
    print(f"  pid {os.getpid()} -- the cue is:  kill -INT -{os.getpid()}\n"
          f"  (once = go to the stop; twice = abort. Ctrl-C works if attached.)")
    print("  validation runs AT THE STOP, not per campaign (section 4)\n")
    for c in entries:
        n, b = trials_run(c["name"]), budget_of(c["name"])
        mark = ("already run" if already_run(c["name"])
                else f"resume at {n}/{b}" if n and b else "to run")
        h = c.get("hypothesis")
        shown = (str(h).replace("config/hypotheses/", "") if h else
                 "(refined from the campaign before it -- not written yet)")
        print(f"  {c['name']:24} {mark:12} {shown}")
    if a.dry_run:
        print("\n  dry run: nothing was run")
        if a.train_only:
            print("  --train-only: phases 2-4 would be skipped, so there is no "
                  "L2 family and\n  no campaign would be validated.")
        else:
            print("  the L2 family would be: " + ", ".join(names))
        return 0

    SESSIONS.mkdir(parents=True, exist_ok=True)
    record = SESSIONS / f"{session}.json"
    global LOGS
    LOGS = SESSIONS / session
    print(f"  per-stage output goes to {LOGS.relative_to(ROOT)}/ in full")
    log: dict = {"schema": "research-session/1", "session": session,
                 "plan": str(a.plan), "declared_family": names,
                 "started": dt.datetime.now(dt.timezone.utc).isoformat(
                     timespec="seconds"),
                 "campaigns": {}, "validation": {}, "l2": None}

    # ---- phase 1: every campaign, through report, with NO validation --------
    print("\nphase 1: campaigns (register, run, nulls, report)\n")
    if a.train_only and a.holdout_if_l2_passes:
        sys.exit("--train-only and --holdout-if-l2-passes contradict each other: "
                 "one says touch no closed split, the other arms the single look.")
    if a.holdout_if_l2_passes:
        print("  the holdout look is ARMED for this session: it will be taken if "
              "L2 PASSES\n  and holdout.py's own refusals all clear. Armed now, "
              "before any validation\n  number exists, which is the only moment "
              "that decision can honestly be made.\n")
    print("  Ctrl-C once = stop proposing and go to the stop (phases 2-4).\n"
          "  Ctrl-C twice = abort.\n")

    stopped_by_cue = False
    aborted_by_failure = False
    completed: list[str] = []
    cur_hypothesis = (ROOT / str(fam["seed"])) if fam else None
    def _refine_after(name: str, parent: pathlib.Path) -> tuple[pathlib.Path, bool]:
        """Write the next campaign's question from this one. `(parent, aborted)`.

        **Called from the normal path AND from the resume path.** It used to
        live inside the stage loop's `else:`, so a `--resume` that skipped a
        completed campaign skipped its refinement too -- and the family then had
        no question for campaign 2, was passed over, and the session validated
        one campaign of a declared three. One definition, two callers.

        **Refining here, inside phase 1, is what makes it legal.**
        `refine_hypothesis.py` refuses if any validation artefact is readable for
        the campaign it is given; during phase 1 none exists, because validation
        runs at the stop. The ordering enforces §4 rather than a promise to be
        careful -- and on a resume AFTER a stop it refuses, which is correct and
        is why that case aborts rather than improvising.
        """
        if not (fam and refines and name != entries[-1]["name"]):
            return parent, False
        if all(e.get("hypothesis") is not None for e in entries if e["name"] != name):
            return parent, False          # every successor already has one
        nxt = next(e for e in entries
                   if e["name"] != name and e.get("hypothesis") is None)
        # **Do NOT pass the campaign's model.** This used to send `c["model"]` --
        # the PROPOSER's model -- so the refiner ran on whatever wrote the
        # strategies and `refine_hypothesis`'s own choice could never take effect
        # inside a session. Same shape as the committee defect in
        # `nq_campaign.py`, where the auditors inherited the proposer's model.
        #
        # The refiner picks its own (mimo-v2.6-pro: one call does the thinking,
        # so 738 s is affordable where 20 calls a campaign would not be).
        # `refine_model:` in the plan overrides it.
        refine_argv = [PY, "scripts/refine_hypothesis.py",
                       "--campaign", name,
                       "--parent", str(parent), "--commit"]
        if fam.get("refine_model"):
            refine_argv += ["--model", str(fam["refine_model"])]
        code, out = _retrying(refine_argv,
                              label=f"{name} refine -> next question",
                              retries=MAX_STAGE_RETRIES)
        written = [ln.split("wrote ", 1)[1].split("  ")[0].strip()
                   for ln in out.splitlines() if ln.startswith("wrote ")]
        if code or not written:
            log["campaigns"][name] = (
                str(log["campaigns"].get(name, "")) + "; refinement FAILED")
            log["refinement_failed_after"] = name
            # **A broken session does not spend validation looks.** This used to
            # set stopped_by_cue, which made an infrastructure failure behave
            # like a human deciding to stop -- and a human deciding to stop means
            # "validate what we have". On 2026-09-30 a single ReadTimeout
            # therefore spent nq-s02-01's one validation look on a session that
            # never executed its plan. A cue is a decision; a
            # timeout is not, and the two must not share a code path.
            print(f"\n  the refinement after {name} failed after "
                  f"{MAX_STAGE_RETRIES} retries, so there is no\n  "
                  f"question for the next campaign.\n  STOPPING WITHOUT VALIDATING. "
                  f"Validation is one look per campaign and this session did "
                  f"not run its plan;\n  spending those looks on a broken "
                  f"session would cost them for nothing.\n  Fix the endpoint "
                  f"and start a fresh family, or cue the stop deliberately if "
                  f"you want these validated.", flush=True)
            return parent, True
        nxt["hypothesis"] = written[0]
        log.setdefault("refinements", []).append(
            {"after": name, "wrote": written[0],
             "machine_written": True, "amendment": 11})
        print(f"      next question: {written[0]}", flush=True)
        return ROOT / written[0], False

    for c in entries:
        if stopped_by_cue or aborted_by_failure:
            log["campaigns"][str(c["name"])] = (
                "not started (human cue)" if stopped_by_cue
                else "not started (the session aborted on a failure)")
            continue
        name = str(c["name"])
        if a.resume and already_run(name):
            print(f"  {name}: already has a ledger, skipping")
            log["campaigns"][name] = "skipped (resume)"
            # **But its refinement still has to run.** Skipping the campaign
            # used to skip this too, so a resumed family never got a question
            # for the campaign after it: on 2026-10-04 nq-s05b-02 and -03 were
            # passed over and the session validated a family of one. The
            # campaign's own question comes from its record rather than from
            # whatever the loop happened to be carrying.
            rec = CAMPAIGNS / name / "campaign.json"
            if rec.is_file():
                hp = (json.loads(rec.read_text()).get("record") or {}
                      ).get("hypothesis_path")
                if hp:
                    cur_hypothesis = ROOT / hp
            cur_hypothesis, aborted_by_failure = _refine_after(
                name, cur_hypothesis)
            record.write_text(json.dumps(log, indent=2) + "\n")
            continue
        try:
            if not registered(name):
                if c.get("hypothesis") is None:
                    log["campaigns"][name] = ("no question: the refinement "
                                              "before it did not produce one")
                    continue
                extra = ["--hypothesis", str(c["hypothesis"])]
                for flag, key in (("--profile", "profile"), ("--n", "n"),
                                  ("--model", "model"),
                                  ("--min-exposed", "min_exposed"),
                                  # Amendment 12. Omitted from a plan means the
                                  # chain, which is what every session before
                                  # 2026-10-04 ran.
                                  ("--generation-width", "generation_width")):
                    if c.get(key) is not None:
                        extra += [flag, str(c[key])]
                # **Amendment 7, and it is a BOOLEAN, so it does not fit the
                # loop above.** A plan could not set it at all: the probe's
                # default lives in `config/run.yaml` (`probe_before_trial:
                # true`) and a session had no way to say otherwise. That was
                # invisible while every campaign ran on NQ, because the probe's
                # synthetic profile could execute an NQ proposal. It cannot
                # execute an options one -- `ctx.options()` raises on a dataset
                # with no options channel -- so the probe would have reported a
                # crash on EVERY proposal of an options campaign and told the
                # agent its own code was at fault. The same shape as the
                # `generation_width` note above: a flag the driver can pass and
                # the plan cannot set is a flag that silently does nothing.
                if c.get("probe") is not None:
                    extra += ["--probe" if c["probe"] else "--no-probe"]
                code, _ = campaign_stage(name, "register", extra)
                if code:
                    log["campaigns"][name] = "register failed"
                    continue
            for stage in ("run", "nulls", "report"):
                code, body = campaign_stage(
                    name, stage,
                    retries=MAX_RUN_RETRIES if stage == "run" else 0,
                    backoff=RUN_BACKOFF_S if stage == "run" else None)
                if code:
                    # **Say WHY, in the record.** nq-s06-03's record said only
                    # "run failed"; the cause -- a gateway ModelSubstituted that
                    # outlasted every retry -- was in a stage log nobody reading
                    # the session record would open. A failure whose reason lives
                    # somewhere else gets diagnosed as something else.
                    why = next((t for t in TRANSIENT if t in body), None)
                    log["campaigns"][name] = (
                        f"{stage} failed"
                        + (f": {why} outlasted {MAX_RUN_RETRIES} retries"
                           if why else ""))
                    log[f"{stage}_failed_after"] = name
                    break
            else:
                log["campaigns"][name] = "complete"
                completed.append(name)
                # **Refine here, inside phase 1, and that is what makes it
                # legal.** refine_hypothesis.py refuses if any validation
                # artefact is readable for the campaign it is given; during
                # phase 1 none exists, because validation runs at the stop. The
                # ordering enforces §4 rather than a promise to be careful.
                cur_hypothesis, aborted_by_failure = _refine_after(
                    name, cur_hypothesis)
        except KeyboardInterrupt:
            # **Section 4's stop, taken by a human who has seen no validation
            # number** -- there are none yet, phase 2 has not run. That is the
            # whole reason this is a legitimate stopping rule and not optional
            # stopping: the thing that would bias it does not exist.
            stopped_by_cue = True
            log["campaigns"][name] = "interrupted mid-flight (human cue)"
            print(f"\n  CUE RECEIVED during {name}. No further campaign starts.\n"
                  f"  {name} is recorded as having run without producing a "
                  f"winner, so its\n  trials are in the count and it is not in "
                  f"the L2 family.\n  Going to the stop: validation, then L2, "
                  f"then the holdout.\n", flush=True)
        record.write_text(json.dumps(log, indent=2) + "\n")

    log["stopped_by_cue"] = stopped_by_cue
    log["declared_family"] = completed
    log["family_note"] = (
        "the L2 family is the campaigns that reached `report` and therefore have "
        "a winner. Campaigns that ran and produced none are in `campaigns` above "
        "with their reason: their trials count, they contribute no candidate, and "
        "dropping them from the family is not family-shrinking because there is "
        "nothing of theirs to drop." if stopped_by_cue else
        "every campaign in the plan, written before the session ran.")
    # **Only a campaign that COMPLETED is validated, cue or no cue.** This read
    # `completed if stopped_by_cue else names`, so a human stop validated what
    # ran and every other ending validated the whole declared family whether it
    # ran or not.
    #
    # On 2026-10-05 nq-s06-03 died at trial 36 of 100 on a gateway
    # `ModelSubstituted`. `nulls` and `report` therefore never ran, so it had no
    # train-side gate at all -- its champion was picked by the carry rule alone,
    # never corrected and never audited -- and it was handed a validation look
    # anyway. Its +0.1371 then became the L2's observed best, over two campaigns
    # that had completed 100 trials each. The L2 failed, so nothing was claimed;
    # had it passed, the pass would have rested on the campaign that did not run.
    #
    # The refinement-failure path learned this on 2026-09-30 -- "a cue is a
    # decision; a timeout is not, and the two must not share a code path" -- and
    # the run-failure path never did. One rule now covers both: validation is for
    # campaigns that executed their pre-registered budget and were gated.
    excluded = [n for n in names if n not in completed]
    names = completed

    if a.train_only:
        log["train_only"] = True
        log["validation"] = ("NOT RUN -- --train-only. This session is a "
                             "measurement about the machinery, not a search for "
                             "a candidate, and validation is one look per "
                             "campaign.")
        log["finished"] = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds")
        record.write_text(json.dumps(log, indent=2) + "\n")
        print(f"\n  --train-only: phases 2-4 SKIPPED. {len(completed)} "
              f"campaign(s) ran and are reported;\n  none is validated, so every "
              f"one of their looks is still available.\n  wrote "
              f"{record.relative_to(ROOT)}")
        # Same rule: `--train-only` asks for phases 2-4 to be skipped, not for
        # zero campaigns. Nothing completed is a failure here too.
        return 0 if (completed or stopped_by_cue) else 1

    if aborted_by_failure:
        log["aborted_by_failure"] = True
        log["validation"] = "NOT RUN -- the session aborted on a failure"
        log["finished"] = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds")
        record.write_text(json.dumps(log, indent=2) + "\n")
        print(f"\n  phases 2-4 were SKIPPED. {len(completed)} campaign(s) ran "
              f"and are reported;\n  none has been validated, so every one of "
              f"their looks is still available.\n  wrote "
              f"{record.relative_to(ROOT)}")
        return 1

    if not names:
        # **Exit non-zero unless a human asked for this.** It returned 0, so a
        # session where NOTHING ran reported success -- which is how the first
        # rehearsal printed "session driver exited 0" over a session that had
        # died at its very first register. For anything automated that is the
        # same class of fault as the ones that cost validation looks: a failure
        # that reads as a pass.
        #
        # The rule is this codebase's own, from the refinement-failure path: a
        # cue is a decision and a failure is not. A human who stopped the session
        # got what they asked for; a session that produced nothing for any other
        # reason did not do its job, whatever its log says.
        why = ("the human cue arrived before any campaign completed"
               if stopped_by_cue else
               "no campaign completed, and nothing asked for that")
        print(f"\n  no campaign produced a winner, so there is nothing to "
              f"validate and no family\n  to correct over: {why}.\n"
              f"  Stopping here.")
        log["finished"] = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds")
        log["produced_nothing"] = why
        record.write_text(json.dumps(log, indent=2) + "\n")
        return 0 if stopped_by_cue else 1

    # ---- phase 2: validation, all of them, now ------------------------------
    #
    # **A truncated family does not get to spend its validation looks.**
    # Validation is one pass per campaign and it cannot be taken back; §4 puts
    # it at the stop precisely so it is taken once, over the family that was
    # declared. On 2026-10-04 a `--resume` skipped nq-s05b-01 as already run,
    # which also skipped the refinement that writes the NEXT question -- so
    # campaigns 2 and 3 had no question, were passed over, and the session fell
    # straight through to here and validated a family of one. The L2 then ran
    # at M = 1 over a declared M of 3.
    #
    # Nothing downstream could detect that: the L2 reports the M it was given.
    # So it is refused here, where the difference is still visible.
    planned = [str(c["name"]) for c in entries]
    missing = excluded if not stopped_by_cue else []
    if missing:
        print("\nphase 2 REFUSED: the declared family is incomplete.\n")
        for n in missing:
            print(f"    {n}: {log['campaigns'].get(n, 'never started')}")
        print("\n  Validation is one look per campaign and §4 puts it at the "
              "stop so it\n  is taken once, over the family that was declared. "
              f"{len(missing)} of {len(planned)}\n  campaigns never ran, so "
              "validating now would spend the looks of a family\n  that does "
              "not exist and compute L2 at the wrong M.\n")
        print("  A human cue (Ctrl-C) is a legitimate stop and validates what "
              "ran.\n  This is not that: the campaigns were passed over "
              "without being asked for.\n")
        log["phase2"] = f"refused: {len(missing)} campaign(s) never ran"
        log["finished"] = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds")
        record.write_text(json.dumps(log, indent=2) + "\n")
        return 1

    print("\nphase 2: validation at the stop -- every winner, in one pass\n")
    print(f"  the family is {len(names)} campaign(s): {', '.join(names)}\n"
          "  a second Ctrl-C from here aborts.\n")
    for name in names:
        if not already_run(name):
            print(f"  {name}: did not finish its budget "
                  f"({trials_run(name)}/{budget_of(name)}), nothing to validate")
            continue
        code, _ = campaign_stage(name, "validate")
        log["validation"][name] = "done" if code == 0 else "failed"
        record.write_text(json.dumps(log, indent=2) + "\n")

    # ---- phase 3: the L2 gate over the declared family ---------------------
    print("\nphase 3: the L2 gate over the declared family\n")
    code, out = _run([PY, "scripts/l2_gate.py", "--campaigns", ",".join(names)],
                     label="l2_gate")
    # **The verdict file must be about THIS family, or it is not this session's
    # result.** `runs/l2-verdict.json` is a single fixed path that every L2 run
    # overwrites. In session-01 the gate exited 1 because no campaign had a
    # winner, and this code then read the file left by an earlier manual run and
    # printed `M = 6, best nq-orderflow-01` as the session's L2 outcome -- a
    # different family's verdict, in a session record, labelled as its own. That
    # is worse than no verdict, and it is the fifth time in this project that a
    # number has reached a consumer it was not about.
    # `_RUNS`, so a rehearsal reads the verdict ITS l2_gate wrote. Hardcoded,
    # this read the real `runs/l2-verdict.json` during a rehearsal and found a
    # verdict about nq-s06 -- which the guard below correctly refused to treat as
    # this session's result, and that refusal is how the bug was visible at all.
    verdict = _RUNS / "l2-verdict.json"
    v = json.loads(verdict.read_text()) if verdict.exists() else None
    got = sorted((v or {}).get("campaigns") or [])
    if code != 0 or v is None or got != sorted(names):
        log["l2"] = {
            "error": f"the L2 gate exited {code} and no verdict about this "
                     f"family was produced.",
            "declared_family": sorted(names),
            "verdict_file_family": got or None,
            "note": ("a verdict file exists but is about a different family, so "
                     "it is NOT this session's result and is not recorded as one"
                     if v is not None and got != sorted(names) else
                     "no verdict file"),
            "stderr_tail": out[-800:],
        }
        print(f"    NO L2 VERDICT for this session.")
        if v is not None and got != sorted(names):
            print(f"      {verdict.relative_to(ROOT)} is about "
                  f"{', '.join(got) or '(no family)'} -- a different family. "
                  f"Not recorded as this session's.")
        record.write_text(json.dumps(log, indent=2) + "\n")
        v = None
    else:
        log["l2"] = v
        record.write_text(json.dumps(log, indent=2) + "\n")

    # `_run` shows one line, and the L2 gate's most important output is not on it.
    # Four times in this project a number has been computed correctly and never
    # reached its consumer; a session log that hides the gate's own warning would
    # be the fifth.
    g = (v.get("gate") or v) if isinstance(v, dict) else {}
    if g.get("verdict") or g.get("withheld"):
        print(f"    L2 verdict        {g.get('verdict') or 'WITHHELD'}  "
              f"M = {g.get('m_campaigns')}  best {g.get('best_campaign')}  "
              f"p = {g.get('p')}")
        print(f"    rule (a) Sortino > 0 on the best   {g.get('rule_a_pass')}")
        print(f"    rule (b) max_of_m p <= q           {g.get('reject')}")
        for c in g.get("per_campaign") or ():
            z = c.get("sds_above_own_null")
            print(f"      {c['campaign_id']:24} "
                  f"{'n/a' if z is None else format(z, '+7.2f')} sd above its own null")
        if g.get("disagreement"):
            print("    !! THE TWO L2 STATISTICS DISAGREE -- see the verdict file "
                  "and Amendment 10.\n       The statistic in force is a "
                  "pre-registration decision, not this script's.")

    # ---- phase 4: the holdout, checked and NOT taken -----------------------
    print("\nphase 4: the holdout -- checked, never taken by a script\n")
    code, out4 = _run([PY, "scripts/holdout.py", "--check"],
                      label="holdout --check")
    log["holdout_check_clear"] = (code == 0)
    if a.holdout_if_l2_passes:
        if code != 0:
            print("\n  ARMED, but the check above is not clear, so nothing was "
                  "opened.\n  Every refusal holdout.py printed still stands.")
            log["holdout"] = "armed; refused by holdout.py --check"
        else:
            print("\n  ARMED and clear. Taking the single look.\n")
            hcode, hout = _run([PY, "scripts/holdout.py",
                                "--reason", a.holdout_if_l2_passes],
                               label="holdout --reason")
            log["holdout"] = ("taken" if hcode == 0 else "attempted; failed")
            log["holdout_reason"] = a.holdout_if_l2_passes
            print(hout[-1600:])
    else:
        log["holdout"] = "checked only; not armed"
        print("\n  §8 gives 73 sessions one look, EVER -- not one per session. "
              "This session was\n  not armed, so nothing was opened. To arm the "
              "next one, pass\n  --holdout-if-l2-passes \"<reason>\" at launch, "
              "which is the only moment the\n  decision can be made without a "
              "validation number in front of you.")

    log["finished"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    record.write_text(json.dumps(log, indent=2) + "\n")
    print(f"\n  wrote {record.relative_to(ROOT)}")
    return 0


def main() -> int:
    try:
        return _main()
    except KeyboardInterrupt:
        # The second cue. The first one is caught inside phase 1 and means "go to
        # the stop"; this one means "stop now", and it must not be swallowed or
        # there is no way out of a wedged validation run.
        print("\n\nABORTED on a second cue. Whatever had been written stays "
              "written;\n nothing was rolled back, because the record is "
              "append-only.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
