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

## What it will not do

**It will not take the holdout look.** It prints what `scripts/holdout.py --check`
would say and stops. §8 gives 73 sessions one look ever, and a driver that could
spend it as the last line of an overnight script is a driver that will spend it by
accident.

**It will not choose the family.** The L2 family is every campaign in the plan,
written into the plan before the session runs, which is the same rule
`l2_gate.py` enforces by making `--campaigns` explicit: dropping the campaigns
that did badly shrinks M and inflates the maximum's significance.

**It will not invent hypotheses.** Each campaign in the plan names a committed
hypothesis file. Amendment 8 makes the commit the human act, so a session cannot
distil and register in one breath.

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
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CAMPAIGNS = ROOT / "runs" / "campaigns"
SESSIONS = ROOT / "runs" / "sessions"
PY = sys.executable


def _run(argv: list[str], *, label: str) -> tuple[int, str]:
    """One stage, streamed to a log and summarised here."""
    t0 = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, cwd=str(ROOT))
    mins = (time.monotonic() - t0) / 60
    tail = (proc.stdout or "").strip().splitlines()
    last = tail[-1] if tail else ""
    print(f"    {label:<34} exit {proc.returncode}  {mins:5.1f}m  {last[:70]}",
          flush=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def campaign_stage(name: str, stage: str, extra: list[str] | None = None):
    return _run([PY, "scripts/nq_campaign.py", "--stage", stage, "--name", name,
                 *(extra or [])], label=f"{name} {stage}")


def already_run(name: str) -> bool:
    return (CAMPAIGNS / name / "ledger.jsonl").exists()


def registered(name: str) -> bool:
    return (CAMPAIGNS / name / "campaign.json").exists()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", required=True, type=pathlib.Path)
    ap.add_argument("--resume", action="store_true",
                    help="skip campaigns that already have a ledger")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and what would run; touch nothing")
    a = ap.parse_args()

    import yaml
    if not a.plan.exists():
        sys.exit(f"no plan at {a.plan}")
    plan = yaml.safe_load(a.plan.read_text())
    session = str(plan.get("session") or a.plan.stem)
    entries = plan.get("campaigns") or []
    if not entries:
        sys.exit("the plan names no campaigns")

    names = [str(c["name"]) for c in entries]
    if len(set(names)) != len(names):
        sys.exit(f"duplicate campaign names in the plan: {names}")

    print(f"session {session}: {len(entries)} campaign(s)")
    print("  validation runs AT THE STOP, not per campaign (section 4)\n")
    for c in entries:
        mark = "already run" if already_run(c["name"]) else "to run"
        print(f"  {c['name']:24} {mark:12} "
              f"{str(c.get('hypothesis','')).replace('config/hypotheses/', '')}")
    if a.dry_run:
        print("\n  dry run: nothing was run")
        print("  the L2 family would be: " + ", ".join(names))
        return 0

    SESSIONS.mkdir(parents=True, exist_ok=True)
    record = SESSIONS / f"{session}.json"
    log: dict = {"schema": "research-session/1", "session": session,
                 "plan": str(a.plan), "declared_family": names,
                 "started": dt.datetime.now(dt.timezone.utc).isoformat(
                     timespec="seconds"),
                 "campaigns": {}, "validation": {}, "l2": None}

    # ---- phase 1: every campaign, through report, with NO validation --------
    print("\nphase 1: campaigns (register, run, nulls, report)\n")
    for c in entries:
        name = str(c["name"])
        if a.resume and already_run(name):
            print(f"  {name}: already has a ledger, skipping")
            log["campaigns"][name] = "skipped (resume)"
            continue
        if not registered(name):
            extra = ["--hypothesis", str(c["hypothesis"])]
            for flag, key in (("--profile", "profile"), ("--n", "n"),
                              ("--model", "model"), ("--min-exposed", "min_exposed")):
                if c.get(key) is not None:
                    extra += [flag, str(c[key])]
            code, _ = campaign_stage(name, "register", extra)
            if code:
                log["campaigns"][name] = "register failed"
                continue
        for stage in ("run", "nulls", "report"):
            code, _ = campaign_stage(name, stage)
            if code:
                log["campaigns"][name] = f"{stage} failed"
                break
        else:
            log["campaigns"][name] = "complete"
        record.write_text(json.dumps(log, indent=2) + "\n")

    # ---- phase 2: validation, all of them, now ------------------------------
    print("\nphase 2: validation at the stop -- every winner, in one pass\n")
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
    verdict = ROOT / "runs" / "l2-verdict.json"
    log["l2"] = json.loads(verdict.read_text()) if verdict.exists() else \
        {"error": out[-600:]}
    record.write_text(json.dumps(log, indent=2) + "\n")

    # ---- phase 4: the holdout, checked and NOT taken -----------------------
    print("\nphase 4: the holdout -- checked, never taken by a script\n")
    _run([PY, "scripts/holdout.py", "--check"], label="holdout --check")
    print("\n  §8 gives 73 sessions one look, ever. A driver that could spend it "
          "as the\n  last line of an overnight script is a driver that will spend "
          "it by accident.\n  If the check above is clear, a human runs "
          "scripts/holdout.py --reason \"...\".")

    log["finished"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    record.write_text(json.dumps(log, indent=2) + "\n")
    print(f"\n  wrote {record.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
