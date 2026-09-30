#!/usr/bin/env python3
"""The single look. 73 sessions, once, ever.

    python scripts/holdout.py --rehearse                  # synthetic, safe, repeatable
    python scripts/holdout.py --check                     # what would block a real look
    python scripts/holdout.py --reason "..."              # the real look. One. Ever.

Pre-registration §8 and Amendment 9. This script exists because the alternative
was a hand-typed `run.py --unlock-holdout` command, and a one-shot that depends on
somebody typing the right profile and remembering to carry a fitted champion's
weights is not a protocol, it is a hope.

## Seven refusals, in the order they fire

Every one is a mechanical check on something §8 or Amendment 9 states in prose.
They are ordered so the cheapest and most decisive come first.

1. **Amendment 9 must be IN FORCE.** §8: "the pass rule is written *before* it is
   opened." Until Michael marks that section in force, there is no pass rule and
   a number from the holdout would have nothing to be judged against.
2. **The L2 gate must have PASSED.** §8: opened "only if the L2 gate accepts. If
   nothing survived L2, opening the holdout confirms nothing and spends 73
   sessions that cannot be recovered."
3. **The candidate must be the L2 gate's recorded best.** Choosing a different one
   after the fact is not a holdout look, it is a second selection.
4. **No look may already have been taken.** Enforced on disk, not in memory:
   `runs/holdout-result.json` existing at all is a refusal, because
   `HoldoutLock` is per-process and a second process would happily unlock again.
5. **A reason is required**, and it lands in the run record. `--unlock-holdout` is
   on `run.py`'s agent-forbidden list; this script is the human path.
6. **A fitted champion must carry weights.** The engine refuses a second `fit()`,
   so a fitted strategy run without `weights_path` scores an untrained model --
   and on the holdout that mistake is unrecoverable.
7. **The L2 gate's two statistics must not disagree.** Added after Task 70, which
   measured the maximum over raw validation Sortino as valid but **blind**: on our
   six real validation nulls, whose standard deviations differ by 20x, it named
   two of the six campaigns the best of M zero times in 4,000 simulated families
   and a third of them once, and it left `nq-orderflow-01` at +17.8 sd above its
   own null while
   recording p = 0.7321 about a campaign at +0.10 sd. `max_of_m` now reports both
   statistics and sets `disagreement` when they differ. **While Amendment 10 --
   which settles which statistic decides -- is not in force**, a disagreement is a
   refusal: there is no second look in which to try the other candidate. Once it
   is signed the document has chosen and this refusal stops firing; the
   disagreement stays in the verdict as a fact about the two statistics rather
   than an open question. Signed 2026-09-30, so it no longer fires today.

## Rehearsal, and why it is not optional

`--rehearse` runs the identical path against the profile named `holdout`, which
is **synthetic** data. Every refusal above is skipped except the weights one,
nothing is recorded as the real look, and it can be run as often as you like.

A one-shot you cannot rehearse is a one-shot you will get wrong. The synthetic
holdout profile is the only reason this script can be tested at all.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# `scripts/` too: running this file puts it on the path automatically, but the
# tests load it by file location, where it is not.
sys.path.insert(0, str(ROOT / "scripts"))

import prereg  # noqa: E402  -- after sys.path is set above

CAMPAIGNS = ROOT / "runs" / "campaigns"
L2_VERDICT = ROOT / "runs" / "l2-verdict.json"
RESULT = ROOT / "runs" / "holdout-result.json"
REHEARSAL = ROOT / "runs" / "holdout-rehearsal.json"
PREREG = ROOT / "docs" / "design" / "pre-registration.md"
LIVE_WEIGHTS = ROOT / "runs" / "current.weights"
STRATEGY = ROOT / "src" / "strategies" / "current.py"
B_NULL = 10_000
Q = 0.05


def _rel(path: pathlib.Path) -> str:
    """A readable path that never raises. `relative_to` throws for anything
    outside the repo, and a refusal message that crashes while explaining itself
    is worse than a long path."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def amendment_9_in_force() -> bool:
    """Whether the pass rule exists yet. Read from the document, not a flag.

    This used to ask "'IN FORCE' present and 'NOT IN FORCE' absent", which reads
    **"REGISTERED, NOT YET IN FORCE"** as in force -- the form Amendments 4 and 7
    are written in. The check guarding the one irrecoverable look would have
    unlocked on a status line that says the opposite in plain English.
    `scripts/prereg.py` requires the status to *begin* with "IN FORCE" instead.

    The text is read here and passed in, rather than letting `prereg` open its
    own copy, so `PREREG` above stays the single place a test redirects.
    """
    try:
        text = PREREG.read_text()
    except OSError:
        return False        # an unreadable protocol is not one anybody signed
    return prereg.in_force(9, text)


def _amendment_10_in_force() -> bool:
    """Whether the L2 statistic has been settled. Same redirect as Amendment 9."""
    try:
        text = PREREG.read_text()
    except OSError:
        return False
    return prereg.in_force(10, text)


def l2_passed() -> tuple[bool, dict]:
    if not L2_VERDICT.exists():
        return False, {}
    v = json.loads(L2_VERDICT.read_text())
    return v.get("verdict") == "PASS", v


def champion_of(campaign: str) -> dict:
    """The campaign's gate winner: its source, digest and weights, from the
    validation record so it is the same candidate L2 ruled on."""
    d = CAMPAIGNS / campaign
    v = json.loads((d / "validation.json").read_text())
    digest = v.get("winner_digest")
    src = d / "sources" / f"{digest}.py"
    if not src.exists():
        src = ROOT / "runs" / "sources" / f"{digest}.py"
    if not src.exists():
        raise FileNotFoundError(f"no stored source for winner {digest}")
    weights = None
    for cand in (d / "weights" / f"{digest}.weights",
                 ROOT / "runs" / "weights" / f"{digest}.weights"):
        if cand.exists():
            weights = cand
            break
    return {"campaign": campaign, "digest": digest, "source": src,
            "weights": weights,
            "validation_primary": (v.get("validation") or {}).get("primary")}


def blocked(rehearse: bool) -> list[str]:
    """Everything that would stop a real look, as readable lines."""
    out: list[str] = []
    if not amendment_9_in_force():
        out.append("Amendment 9 is not IN FORCE: §8 requires the pass rule to be "
                   "written before the holdout is opened, and until that section "
                   "says IN FORCE there is nothing to judge a number against.")
    ok, v = l2_passed()
    if not v:
        out.append(f"no L2 verdict at {_rel(L2_VERDICT)}: run "
                   "scripts/l2_gate.py with a declared family first.")
    elif not ok:
        out.append(f"the L2 gate did not pass (verdict {v.get('verdict')}, "
                   f"rule (a) {v.get('rule_a_pass')}, p {v.get('p')}). §8: opened "
                   "only if the L2 gate accepts -- otherwise the look confirms "
                   "nothing and spends 73 sessions that cannot be recovered.")
    if v and v.get("disagreement") and not _amendment_10_in_force():
        raw = (v.get("raw_view") or {}).get("best_campaign")
        std = (v.get("standardised_view") or {}).get("best_campaign")
        out.append(
            "the L2 gate's two statistics disagree about the family. The raw "
            f"maximum, which is the one in force, names {raw}; the standardised "
            f"maximum names {std}. Task 70 measured the raw maximum as valid but "
            "blind, and Amendment 10 -- which settles which statistic decides -- "
            "is not in force. The single look must not be spent on a candidate "
            "the gate itself cannot agree is the best of M: there is no second "
            "look in which to try the other one. Once Amendment 10 is signed the "
            "document has chosen, and this refusal stops firing -- the "
            "disagreement is still recorded in the verdict, as a fact about the "
            "two statistics rather than an unresolved question.")
    if RESULT.exists():
        out.append(f"{_rel(RESULT)} already exists. The look has been "
                   "taken. There is no second one, under any justification.")
    return out


def run_once(champ: dict, *, profile: str, reason: str | None,
             ledger: pathlib.Path) -> dict:
    """One `run.py` invocation on a holdout profile. Restores everything after."""
    original = STRATEGY.read_text()
    STRATEGY.write_text(champ["source"].read_text())
    if champ["weights"] is not None:
        LIVE_WEIGHTS.parent.mkdir(parents=True, exist_ok=True)
        LIVE_WEIGHTS.write_bytes(champ["weights"].read_bytes())
    argv = [sys.executable, "run.py", "--human", "--quiet", "--purpose", "check",
            "--profile", profile, "--ledger", str(ledger)]
    if reason:
        argv += ["--unlock-holdout", reason]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, cwd=str(ROOT))
    finally:
        STRATEGY.write_text(original)
        LIVE_WEIGHTS.unlink(missing_ok=True)
    rows = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()] \
        if ledger.exists() else []
    if not rows:
        raise RuntimeError(f"the holdout run produced no ledger row.\n"
                           f"{proc.stdout[-1500:]}\n{proc.stderr[-2000:]}")
    row = rows[-1]
    if row.get("strategy_digest") != champ["digest"]:
        raise RuntimeError(
            f"ran digest {row.get('strategy_digest')} but the champion is "
            f"{champ['digest']}. Refusing to record a look at a different "
            "strategy than the one L2 ruled on.")
    return row


def holdout_null(row: dict, profile_name: str) -> list[float]:
    """The champion's own sign-flip null, on the holdout sessions.

    Amendment 9 rule (ii). Built the way every other null in this project is:
    resample the round turns of the run that just happened.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from replay import load_fill_log
    from src.core.engine.reconstruct import reconstruct
    from src.core.scoring import metrics
    from src.core.scoring.randomization import round_turn as rt
    from src.core.types import Split
    import run as runner
    import yaml

    cfg = yaml.safe_load((ROOT / "config" / "run.yaml").read_text())
    profile = cfg["profiles"][profile_name]
    fills = load_fill_log(row)
    if not fills:
        return []
    dataset = runner.build_dataset(cfg, profile["dataset"], profile["seed"])
    dataset.unlock_holdout("scoring the pre-registered single look (Amendment 9)")
    sessions = dataset.scored_sessions(Split.HOLDOUT,
                                       profile.get("warmup_sessions", 0))
    calendar = metrics.session_calendar(dataset, sessions)
    blotter = reconstruct(
        fills, commission_per_contract=profile.get("commission_per_contract", 0.01))
    return rt.null_sample(blotter, calendar, b=B_NULL, seed=73_073)


def judge(row: dict, null: list[float]) -> dict:
    """Amendment 9's two pass conditions, and the evidence beside them."""
    import numpy as np
    score = row.get("score") or {}
    primary = score.get("primary")
    rule_i = primary is not None and primary > 0
    if null:
        arr = np.asarray(null, dtype=float)
        exceed = int((arr >= primary).sum())
        p = (1 + exceed) / (len(arr) + 1)
    else:
        p = None
    rule_ii = p is not None and p <= Q
    return {
        "rule_i_sortino_positive": bool(rule_i),
        "rule_ii_p": p,
        "rule_ii_reject": bool(rule_ii),
        "q": Q,
        "confirmed": bool(rule_i and rule_ii),
        "evidence_not_thresholds": {
            "primary": primary,
            "primary_uncharged": score.get("primary_uncharged"),
            "trade_count": score.get("trade_count"),
            "n_sessions": score.get("n_sessions"),
            "net_pnl": score.get("net_pnl"),
            "diagnostics": row.get("diagnostics"),
        },
        "note": ("Amendment 9: rules (i) and (ii) are the pass conditions. "
                 "Everything under evidence_not_thresholds is evidence and not a "
                 "threshold -- there is no constant to tune here."),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true",
                   help="report what would block a real look, and stop")
    g.add_argument("--rehearse", action="store_true",
                   help="the identical path on SYNTHETIC holdout data. Repeatable, "
                        "records nothing as the real look.")
    g.add_argument("--reason", help="THE REAL LOOK. One. Ever. Recorded.")
    ap.add_argument("--campaign", help="override the champion's campaign "
                                       "(rehearsal only)")
    a = ap.parse_args()

    reasons = blocked(rehearse=bool(a.rehearse))
    if a.check:
        if not reasons:
            print("nothing blocks a real look. Read Amendment 9 again before "
                  "taking it.")
            return 0
        print(f"{len(reasons)} thing(s) block a real holdout look:\n")
        for r in reasons:
            print(f"  - {r}\n")
        return 1

    if a.reason and reasons:
        print(f"REFUSED. {len(reasons)} thing(s) block this look:\n")
        for r in reasons:
            print(f"  - {r}\n")
        print("Nothing was opened and nothing was written.")
        return 1

    # Which candidate. For a real look it is the L2 gate's recorded best and
    # nothing else; a rehearsal may name one, since it touches no real data.
    _, v = l2_passed()
    campaign = a.campaign if (a.rehearse and a.campaign) else v.get("best_campaign")
    if not campaign:
        sys.exit("no candidate: the L2 verdict names no best campaign "
                 "(and --campaign is rehearsal-only)")
    champ = champion_of(campaign)

    fitted = champ["weights"] is not None
    if a.rehearse:
        profile = "holdout"          # SYNTHETIC. The rehearsal surface.
        if fitted:
            sys.exit("the synthetic holdout profile has no weights_path, so a "
                     "fitted champion cannot be rehearsed on it. Add a fitted "
                     "synthetic holdout profile before rehearsing this champion.")
    else:
        profile = "nq-holdout-fitted" if fitted else "nq-holdout"

    print(f"{'REHEARSAL (synthetic)' if a.rehearse else 'THE SINGLE LOOK'}")
    print(f"  champion         {champ['digest']} from {campaign}")
    print(f"  validation       {champ['validation_primary']}")
    print(f"  fitted           {fitted}"
          + (f", carrying {_rel(champ['weights'])}" if fitted else ""))
    print(f"  profile          {profile}")
    if not a.rehearse:
        print(f"  reason           {a.reason}")

    ledger = (ROOT / "runs" / ("holdout-rehearsal.jsonl" if a.rehearse
                               else "holdout.jsonl"))
    # **A rehearsal unlocks too, and must.** The lock is what the real look turns,
    # so a rehearsal that skips it tests everything except the thing most likely to
    # go wrong -- the first attempt refused with HoldoutLocked and scored nothing.
    # Harmless here: the synthetic dataset is generated per process and its lock is
    # per instance, so unlocking it costs nothing and can be repeated.
    reason = a.reason or ("REHEARSAL on synthetic data -- not the real look "
                          "(scripts/holdout.py --rehearse)")
    row = run_once(champ, profile=profile, reason=reason, ledger=ledger)
    null = holdout_null(row, profile)
    verdict = judge(row, null)

    print(f"\n  score            {(row.get('score') or {}).get('primary')}")
    print(f"  null draws       {len(null)}")
    print(f"  rule (i)  Sortino > 0     {verdict['rule_i_sortino_positive']}")
    print(f"  rule (ii) p <= {Q}        {verdict['rule_ii_reject']}  "
          f"(p = {verdict['rule_ii_p']})")
    print(f"  CONFIRMED        {verdict['confirmed']}")
    print("\n  Confirmed means both rules held. It is not a promotion: that is a "
          "human\n  decision and no verdict in this project means approval.")

    out = {"schema": "holdout-result/1",
           "rehearsal": bool(a.rehearse),
           "taken": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "campaign": campaign, "digest": champ["digest"], "profile": profile,
           "fitted": fitted, "reason": None if a.rehearse else a.reason,
           "l2_verdict": v.get("verdict"), "l2_family": v.get("declared_family"),
           **verdict}
    (REHEARSAL if a.rehearse else RESULT).write_text(
        json.dumps(out, indent=2, default=str) + "\n")
    print(f"\n  wrote {_rel(REHEARSAL if a.rehearse else RESULT)}")
    if not a.rehearse:
        print("  THE HOLDOUT IS SPENT. There is no second look.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
