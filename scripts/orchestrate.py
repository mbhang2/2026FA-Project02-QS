#!/usr/bin/env python3
"""The outer loop: what should the next campaign ask? Evidence, not an LLM.

    .venv/bin/python scripts/orchestrate.py
    .venv/bin/python scripts/orchestrate.py --out runs/orchestration.json
    .venv/bin/python scripts/orchestrate.py --run      # refuses, and says why

Gowtam's `researcher/orchestrator.py` is the only automated layer anyone built
for choosing the next question, and the capability is genuinely missing from
this repo: ours comes from a human plus a paper. This is that idea with three
changes, two of which are hard refusals.

## It never registers or runs a campaign

His `full_run` defaults to `MAX_CAMPAIGNS_PER_LOOP = 5`,
`max_iterations = 2` and `n_trials = trials or 10`: up to 100 unattended
trials, in campaigns of ten. **Section 2 of the pre-registration says a
campaign is "at least N = 400".** Ten is forty times below the floor, and his
trials do count -- they carry `purpose: "trial"`, correctly -- so the loop
spends honest looks at machine speed on campaigns that could not be registered
under our protocol. His own record is four campaigns, 32 trials, 20 crashed,
best score +0.0185.

So `--run` is refused here, by `MIN_TRIALS_PER_CAMPAIGN` and by a second check
on who asked. **How many looks a machine may spend unattended is a
pre-registration question, not a flag**, and until there is an amendment this
script stops at a recommendation a human acts on. That is the same place
`scripts/retrieve.py` stops, for the same reason (Amendment 8).

## What it reads is whitelisted, and fails closed

`src.core.research.trajectory` -- score DIRECTION, never magnitude; no
p-value, verdict, N, null, validation or holdout. An unexpected key raises.

## What it ranks on

Three signals, all already on disk and none needing a gateway call:

* **the deferral list** -- `scripts/open_questions.py`: objections the four
  auditors raised on campaigns that have no validation artefact, which §4
  makes the only ones readable. A question that answers one of these is worth
  more than a fresh one.
* **BWIS source priors** -- `config/bwis-priors.json`, calibrated from the 532
  screened papers. A backlog paper from a source whose interval sits above the
  corpus baseline outranks one from a source below it.
* **the backlog itself** -- papers rated `gate_relevance: high` and feasible
  that were never distilled. 25 of them, with full text already fetched.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.research import bwis  # noqa: E402
from src.core.research.trajectory import (  # noqa: E402
    Trajectory, direction, to_trajectory,
)

#: Section 2. A campaign below this cannot be registered, so the loop may not
#: propose running one and `--run` may not start one.
MIN_TRIALS_PER_CAMPAIGN = 400

REGISTRY = ROOT / "data" / "papers" / "registry.json"
CAMPAIGNS = ROOT / "runs" / "campaigns"


# --------------------------------------------------------------------------- #
# trajectories, through the whitelist
# --------------------------------------------------------------------------- #
def read_trajectories() -> list[Trajectory]:
    """One row per campaign that has a ledger, built field by field.

    **Built explicitly rather than passed through.** A ledger row has 45 keys
    and `to_trajectory` would refuse it; that refusal is the design, so the
    translation happens here where each field can be justified.

    Train only, and by construction: this reads `ledger.jsonl`, which is the
    train ledger. A campaign's validation lives in `validation.jsonl` and is
    never opened here -- §4.
    """
    out: list[Trajectory] = []
    for d in sorted(p for p in CAMPAIGNS.iterdir() if p.is_dir()):
        ledger = d / "ledger.jsonl"
        if not ledger.is_file():
            continue
        rows = []
        for line in ledger.read_text().splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if not rows:
            continue
        scored = [r for r in rows if (r.get("score") or {}).get("primary") is not None
                  and not (r.get("score") or {}).get("rejected")]
        # Direction only: first scored trial against the best one. Neither
        # number leaves this function.
        first = scored[0]["score"]["primary"] if scored else None
        best = max((r["score"]["primary"] for r in scored), default=None)
        turn_first = (scored[0]["score"].get("trade_count") if scored else None)
        turn_last = (scored[-1]["score"].get("trade_count") if scored else None)
        out.append(to_trajectory({
            "campaign_id": d.name,
            "outcome": "completed" if scored else "no_scored_trial",
            "trials": len(rows),
            "scored": len(scored),
            "sessions": (scored[0]["score"].get("n_sessions") if scored else None),
            "score_dir": direction(first, best, flat_within=1e-6),
            "turnover_dir": direction(turn_first, turn_last, flat_within=0.5),
        }))
    return out


# --------------------------------------------------------------------------- #
# the deferral list
# --------------------------------------------------------------------------- #
def read_deferrals() -> list[dict]:
    """Unresolved auditor objections, via the committed script."""
    r = subprocess.run([sys.executable, "scripts/open_questions.py", "--json"],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return []
    try:
        body = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    rows = body.get("questions") or []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


# --------------------------------------------------------------------------- #
# the backlog, ranked
# --------------------------------------------------------------------------- #
#: Words that tie a paper to a theme the auditors keep objecting about. Crude
#: on purpose: a keyword overlap is checkable and an embedding is not, and
#: this output is read by a human who can see the paper title next to it.
THEME_WORDS = {
    "concentration": ("concentration", "tail", "hindsight", "selection",
                      "robustness", "luck", "overfit", "p-hacking"),
    "denominator": ("sharpe", "sortino", "t-statistic", "t value", "denominator",
                    "small sample", "multiple testing", "inflat"),
    "construct": ("volume profile", "point of control", "value area",
                  "volume node", "order flow", "imbalance"),
}


def rank_backlog(cal: bwis.Calibration, deferrals: list[dict]) -> list[dict]:
    papers = list(json.loads(REGISTRY.read_text())["papers"].values())
    pool = [p for p in papers
            if p.get("gate_relevance") == "high"
            and p.get("feasibility") in bwis.USABLE
            and not p.get("hypothesis_path")]
    seen: set[str] = set()
    themes = " ".join(str(d.get("objection") or d.get("summary") or d)
                      for d in deferrals).lower()
    out = []
    for p in pool:
        title = str(p.get("title") or "")
        if title in seen:
            continue
        seen.add(title)
        prior = cal.prior_for(str(p.get("source") or "unknown"))
        blob = (title + " " + str(p.get("claim") or "")).lower()
        hits = sorted({k for k, words in THEME_WORDS.items()
                       if any(w in blob for w in words)
                       and any(w in themes for w in words)})
        out.append({
            "id": p.get("id"),
            "title": title[:110],
            "source": p.get("source"),
            "feasibility": p.get("feasibility"),
            "has_fulltext": bool(p.get("fulltext_fetched")),
            "bwis_band": bwis.band(prior, baseline=cal.baseline),
            "bwis_n": prior.n_observed,
            "answers_deferral": hits,
        })
    rank = {"high": 0, "mid": 1, "low": 2}
    out.sort(key=lambda r: (-len(r["answers_deferral"]),
                            rank.get(r["bwis_band"], 3),
                            not r["has_fulltext"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--run", action="store_true",
                    help="register and run the recommended campaign (refused)")
    ap.add_argument("--trials", type=int, default=0)
    a = ap.parse_args()

    if a.run:
        sys.exit(
            "--run is refused, and not because of a missing flag.\n\n"
            f"  Section 2 of the pre-registration sets a campaign at "
            f"N >= {MIN_TRIALS_PER_CAMPAIGN}; the loop this is modelled on "
            f"defaulted to 10, which is forty times below it, and its trials "
            f"count toward global_n like anyone's.\n"
            "  How many looks a machine may spend unattended is a "
            "pre-registration question. Until that amendment exists this "
            "script stops at a recommendation and a human registers, which is "
            "where scripts/retrieve.py stops for the same reason.\n"
            "  The LLM gateway is also over budget, so no proposal could be "
            "written tonight even if the protocol allowed it.")
    if a.trials and a.trials < MIN_TRIALS_PER_CAMPAIGN:
        sys.exit(f"--trials {a.trials} is below section 2's floor of "
                 f"{MIN_TRIALS_PER_CAMPAIGN}")

    cal = bwis.read()
    trajectories = read_trajectories()
    deferrals = read_deferrals()
    backlog = rank_backlog(cal, deferrals)

    print(f"priors    {bwis.CONFIG_PATH.relative_to(ROOT)} "
          f"digest {cal.digest}  baseline {cal.baseline:.4f} "
          f"on {cal.n_papers} papers")
    print(f"campaigns {len(trajectories)} with a ledger, read through the "
          f"whitelist (direction only)")
    print(f"deferrals {len(deferrals)} unresolved auditor objections")
    print(f"backlog   {len(backlog)} papers rated high and feasible, "
          f"never distilled\n")

    print(f"{'#':>3} {'band':<6}{'src':<10}{'ft':<4}{'answers':<16}title")
    for i, r in enumerate(backlog[:12], 1):
        print(f"{i:>3} {r['bwis_band']:<6}{str(r['source'])[:9]:<10}"
              f"{'yes' if r['has_fulltext'] else 'no':<4}"
              f"{','.join(r['answers_deferral'])[:15]:<16}{r['title'][:62]}")

    body = {
        "schema": "orchestration/1",
        "priors_digest": cal.digest,
        "baseline": cal.baseline,
        "n_campaigns_read": len(trajectories),
        "n_deferrals": len(deferrals),
        "trajectories": [t.__dict__ if hasattr(t, "__dict__") else {
            k: getattr(t, k) for k in t.__slots__} for t in trajectories],
        "backlog": backlog,
        "ran_a_campaign": False,
        "why_not": f"section 2 floor is N >= {MIN_TRIALS_PER_CAMPAIGN}; "
                   f"unattended N spend needs an amendment; gateway over budget",
    }
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(body, indent=2, default=str) + "\n")
        # `relative_to` raises when --out points outside the repo, which a
        # harness writing to a scratch directory legitimately does.
        try:
            shown = a.out.relative_to(ROOT)
        except ValueError:
            shown = a.out
        print(f"\nwrote {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
