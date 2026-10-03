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

CAMPAIGNS = ROOT / "runs" / "campaigns"
SESSIONS = ROOT / "runs" / "sessions"
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


def _retrying(argv: list[str], *, label: str, retries: int = 0):
    """Run it, and try again on a fault that is the endpoint's rather than ours.

    Used by the campaign stages AND by the refinement. The refinement was left
    out of this on 2026-09-30 and a single `ReadTimeout` on the gateway preflight
    ended a five-campaign session after one campaign.
    """
    code, body = 1, ""
    for attempt in range(retries + 1):
        code, body = _run(argv, label=label + (f" (retry {attempt})"
                                               if attempt else ""))
        if code == 0 or attempt == retries:
            return code, body
        hit = next((t for t in TRANSIENT if t in body), None)
        if hit is None:
            return code, body
        wait = STAGE_BACKOFF_S[min(attempt, len(STAGE_BACKOFF_S) - 1)]
        print(f"      {hit} is the endpoint's fault, not ours. Waiting {wait}s, "
              f"then retrying.", flush=True)
        time.sleep(wait)
    return code, body


def campaign_stage(name: str, stage: str, extra: list[str] | None = None,
                   *, retries: int = 0):
    return _retrying([PY, "scripts/nq_campaign.py", "--stage", stage,
                      "--name", name, *(extra or [])],
                     label=f"{name} {stage}", retries=retries)


def already_run(name: str) -> bool:
    return (CAMPAIGNS / name / "ledger.jsonl").exists()


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
                    "min_exposed": fam.get("min_exposed")}
                   for k in range(iterations)]
    if not entries:
        sys.exit("the plan names no campaigns")

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
        mark = "already run" if already_run(c["name"]) else "to run"
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
                                  ("--min-exposed", "min_exposed")):
                    if c.get(key) is not None:
                        extra += [flag, str(c[key])]
                code, _ = campaign_stage(name, "register", extra)
                if code:
                    log["campaigns"][name] = "register failed"
                    continue
            for stage in ("run", "nulls", "report"):
                code, _ = campaign_stage(
                    name, stage,
                    retries=MAX_STAGE_RETRIES if stage == "run" else 0)
                if code:
                    log["campaigns"][name] = f"{stage} failed"
                    break
            else:
                log["campaigns"][name] = "complete"
                completed.append(name)
                # **Refine here, inside phase 1, and that is what makes it
                # legal.** refine_hypothesis.py refuses if any validation
                # artefact is readable for the campaign it is given; during
                # phase 1 none exists, because validation runs at the stop. The
                # ordering enforces §4 rather than a promise to be careful.
                if fam and refines and name != entries[-1]["name"]:
                    nxt = next(e for e in entries
                               if e["name"] != name
                               and e.get("hypothesis") is None)
                    # **Do NOT pass the campaign's model.** This used to send
                    # `c["model"]` -- the PROPOSER's model -- so the refiner ran
                    # on whatever wrote the strategies, and `refine_hypothesis`'s
                    # own choice could never take effect inside a session. Same
                    # shape as the committee defect in `nq_campaign.py`, where
                    # the auditors inherit the proposer's model too.
                    #
                    # The refiner picks its own (mimo-v2.6-pro: one call does the
                    # thinking, so 738 s is affordable where 20 calls a campaign
                    # would not be). `refine_model:` in the plan overrides it.
                    refine_argv = [PY, "scripts/refine_hypothesis.py",
                                   "--campaign", name,
                                   "--parent", str(cur_hypothesis), "--commit"]
                    if fam.get("refine_model"):
                        refine_argv += ["--model", str(fam["refine_model"])]
                    code, out = _retrying(
                        refine_argv,
                        label=f"{name} refine -> next question",
                        retries=MAX_STAGE_RETRIES)
                    written = [ln.split("wrote ", 1)[1].split("  ")[0].strip()
                               for ln in out.splitlines() if ln.startswith("wrote ")]
                    if code or not written:
                        log["campaigns"][name] += "; refinement FAILED"
                        log["refinement_failed_after"] = name
                        # **A broken session does not spend validation looks.**
                        # This used to set stopped_by_cue, which made an
                        # infrastructure failure behave like a human deciding to
                        # stop -- and a human deciding to stop means "validate
                        # what we have". On 2026-09-30 a single ReadTimeout
                        # therefore spent nq-s02-01's one validation look on a
                        # session that never executed its plan. A cue is a
                        # decision; a timeout is not, and the two must not share
                        # a code path.
                        aborted_by_failure = True
                        print(f"\n  the refinement after {name} failed after "
                              f"{MAX_STAGE_RETRIES} retries, so there is no "
                              f"question for the next campaign.\n"
                              f"  STOPPING WITHOUT VALIDATING. Validation is one "
                              f"look per campaign and this session did not run "
                              f"its plan;\n  spending those looks on a broken "
                              f"session would cost them for nothing. The "
                              f"{len(completed)} campaign(s) that ran keep their "
                              f"looks unspent.\n  Fix the endpoint and start a "
                              f"fresh family, or cue the stop deliberately if "
                              f"you want these validated.",
                              flush=True)
                    else:
                        nxt["hypothesis"] = written[0]
                        cur_hypothesis = ROOT / written[0]
                        log.setdefault("refinements", []).append(
                            {"after": name, "wrote": written[0],
                             "machine_written": True,
                             "amendment": 11})
                        print(f"      next question: {written[0]}", flush=True)
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
    names = completed if stopped_by_cue else names

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
        return 0

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
        print("\n  no campaign produced a winner, so there is nothing to "
              "validate and no family\n  to correct over. Stopping here.")
        log["finished"] = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds")
        record.write_text(json.dumps(log, indent=2) + "\n")
        return 0

    # ---- phase 2: validation, all of them, now ------------------------------
    print("\nphase 2: validation at the stop -- every winner, in one pass\n")
    print(f"  the family is {len(names)} campaign(s): {', '.join(names)}\n"
          "  a second Ctrl-C from here aborts.\n")
    for name in names:
        if not already_run(name):
            print(f"  {name}: no ledger, nothing to validate")
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
    verdict = ROOT / "runs" / "l2-verdict.json"
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
