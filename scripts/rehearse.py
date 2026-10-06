#!/usr/bin/env python3
"""Rehearse a whole research session against synthetic data. Amendment 13.

    python scripts/rehearse.py
    python scripts/rehearse.py --campaigns 2 --n 4 --width 2 --keep

**Why this exists.** Every defect that has cost this project something
irreversible was found by running a real session: a timeout that spent a
validation look, a `--resume` that skipped the refinement and spent three more,
a parallel merge that dropped `fills/` and made `n_nulls = 0`, a
`generation_width` that never reached the registration, a probe armed on one
agent of four, a failed run that validated anyway. **Not one of them is
data-dependent.** The most dangerous single act in the protocol -- the holdout --
has been rehearsable since §8 was written. The thing we do every night was not.

## What makes this not a look

Three things, and the first is the justification:

1. it runs on `synthetic` data, and N is looks taken at **our market data**;
2. its output lives under `.scratch/`, which `runlog.ledger_paths()` does not
   read, so `global_n()` cannot see it by construction;
3. every row is `purpose: check`, derived in `nq_campaign.stage_run` from the
   committed profile rather than from a flag.

The profile is fixed here in code and is not an argument, for the same reason
`run.py` takes no dataset argument.

## What a green rehearsal does and does not mean

It means the orchestration executed: every campaign registered, ran its budget,
computed nulls, produced a gate verdict, was offered to the committee, wrote the
next question, and reached validation and L2. **It is checked rather than
assumed** -- the assertions below fail a rehearsal that skipped a stage, because
a green rehearsal that quietly skipped the nulls would be worse than none.

It does **not** mean the next real session will succeed. A gateway substitution,
a model that writes different code against a real question, a dataset fault --
none of those appear here. The claim is only that faults in the shape of the six
above are caught before they cost anything.

**No result here says anything about the market.** The data is synthetic with a
planted signal; its p-values, champions and L2 verdict are about the machinery.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".scratch" / "rehearsal"
PROFILE = "rehearsal"          # fixed in code, never an argument
PY = sys.executable


def _plan(path: pathlib.Path, *, campaigns: int, n: int, width: int,
          seed: pathlib.Path, model: str) -> pathlib.Path:
    path.write_text(
        "# Written by scripts/rehearse.py. Synthetic data; Amendment 13.\n"
        "session: rehearsal\n\nfamily:\n"
        f"  seed: {seed.relative_to(ROOT)}\n"
        f"  iterations: {campaigns}\n"
        "  refine: true\n"
        f"  n: {n}\n"
        f"  generation_width: {width}\n"
        f"  model: {model}\n"
        f"  profile: {PROFILE}\n"
        "  prefix: reh\n")
    return path


def check(campaigns: int) -> list[str]:
    """What must be true of a rehearsal that really exercised the chain.

    Returns the failures. A green run with an empty list is the only green.
    """
    bad: list[str] = []
    camps = sorted((SCRATCH / "campaigns").glob("reh-*"))
    if len(camps) < 1:
        return [f"no campaign directories under {SCRATCH}"]
    for d in camps:
        rec = d / "campaign.json"
        if not rec.is_file():
            bad.append(f"{d.name}: never registered"); continue
        r = json.loads(rec.read_text()).get("record") or {}
        if r.get("profile") != PROFILE:
            bad.append(f"{d.name}: ran on {r.get('profile')!r}, not {PROFILE!r}")
        led = d / "ledger.jsonl"
        rows = [json.loads(x) for x in led.read_text().splitlines()
                if x.strip()] if led.is_file() else []
        if len(rows) < (r.get("budget") or 0):
            bad.append(f"{d.name}: {len(rows)} of {r.get('budget')} trials")
        # Amendment 13's own invariant, checked rather than trusted
        wrong = [x for x in rows if x.get("purpose") != "check"]
        if wrong:
            bad.append(f"{d.name}: {len(wrong)} row(s) not purpose=check -- "
                       f"a rehearsal that writes trial rows IS a look")
        # the merge, which is what dropped fills in nq-s05b-01
        if rows and not any((d / "fills").glob("*")):
            bad.append(f"{d.name}: no fills merged back from the workers")
        # the stages after the run
        nulls = d / "nulls.json"
        if not nulls.is_file():
            bad.append(f"{d.name}: nulls never computed")
        else:
            ne = [x for x in json.loads(nulls.read_text())["samples"] if x]
            if not ne:
                bad.append(f"{d.name}: every null sample is EMPTY -- this is "
                           f"the nq-s05b-01 defect, p would be 1.0")
        if not (d / "tier2-summary.json").is_file():
            bad.append(f"{d.name}: no gate verdict")
        if not (d / "committee-verdict.json").is_file():
            bad.append(f"{d.name}: the committee was never asked")
        # Amendment 7, which was armed on one agent in four
        if r.get("probe_before_trial"):
            pl = d / "probe-ledger.jsonl"
            pn = len([x for x in pl.read_text().splitlines() if x.strip()]) \
                if pl.is_file() else 0
            if pn < len(rows):
                bad.append(f"{d.name}: probe ran on {pn} of {len(rows)} "
                           f"proposals -- Amendment 7 is not reaching them all")
    # the refinement chain, which a --resume once skipped silently
    sess = SCRATCH / "sessions" / "rehearsal.json"
    if not sess.is_file():
        bad.append("no session record")
    else:
        log = json.loads(sess.read_text())
        refs = log.get("refinements") or []
        if campaigns > 1 and len(refs) < campaigns - 1:
            bad.append(f"{len(refs)} refinement(s) for {campaigns} campaigns -- "
                       f"the chain did not write every next question")
        if not (log.get("l2") or {}).get("verdict"):
            bad.append("no L2 verdict")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaigns", type=int, default=2)
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--width", type=int, default=2)
    ap.add_argument("--seed-hypothesis", type=pathlib.Path,
                    default=ROOT / "config" / "hypotheses" / "intraday-continuation.md")
    ap.add_argument("--model", default="deepseek-v4-pro",
                    help="the proposer to rehearse with. The MODEL is an "
                         "argument and the PROFILE is not, on purpose: which "
                         "model answers is an endpoint fact that changes hourly "
                         "-- 2026-10-05, deepseek-v4-pro was substituted even at "
                         "the preflight prompt -- while which data is looked at "
                         "is a protocol fact and must not be selectable.")
    ap.add_argument("--keep", action="store_true",
                    help="leave .scratch/rehearsal in place for inspection")
    ap.add_argument("--check-only", action="store_true",
                    help="re-run the assertions over the last rehearsal")
    a = ap.parse_args()

    if a.check_only:
        bad = check(a.campaigns)
        for b in bad:
            print(f"  FAIL  {b}")
        print(f"\n{'REHEARSAL FAILED' if bad else 'rehearsal clean'}")
        return 1 if bad else 0

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    (SCRATCH / "sessions").mkdir(parents=True)
    plan = _plan(SCRATCH / "plan.yaml", campaigns=a.campaigns, n=a.n,
                 width=a.width, seed=a.seed_hypothesis, model=a.model)

    print(f"rehearsing: {a.campaigns} campaign(s), N={a.n}, width={a.width}, "
          f"profile={PROFILE} (synthetic)")
    print(f"  output   {SCRATCH.relative_to(ROOT)}  -- outside runs/, so "
          f"global_n() cannot see it")
    print(f"  trials   purpose=check, so they are not looks (Amendment 13)\n")

    env = {**os.environ, "PRACTICUM_RUNS_ROOT": str(SCRATCH)}
    code = subprocess.run(
        [PY, "-u", "scripts/research_session.py", "--plan", str(plan)],
        cwd=str(ROOT), env=env).returncode

    print(f"\nsession driver exited {code}; checking what actually ran\n")
    bad = check(a.campaigns)
    for b in bad:
        print(f"  FAIL  {b}")
    if not bad:
        print("  every campaign registered, ran its budget, computed nulls,")
        print("  produced a gate verdict, was offered to the committee, and")
        print("  the chain wrote every next question and reached L2.")
    print(f"\n{'REHEARSAL FAILED' if bad else 'REHEARSAL CLEAN'} "
          f"-- nothing here says anything about the market.")
    if not a.keep and not bad:
        shutil.rmtree(SCRATCH)
    elif bad:
        print(f"  left {SCRATCH.relative_to(ROOT)} in place for inspection")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
