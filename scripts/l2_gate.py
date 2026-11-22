#!/usr/bin/env python3
"""The L2 gate: build each winner's validation null, then correct over M campaigns.

    python scripts/l2_gate.py --list
    python scripts/l2_gate.py --campaigns nq-ml-03,nq-flowcont-5m --dry-run
    python scripts/l2_gate.py --campaigns nq-ml-03,nq-flowcont-5m

Design: `docs/design/pre-registration.md` §4 and §5. The statistic is
`src.core.scoring.correction.max_of_m`, which existed before this script and
could not be run, because two inputs were missing: a null for each winner's
**validation** score, and something to assemble the M winners.

## Why M is declared and never discovered

`--campaigns` is required and takes an explicit list. The script will tell you
what the candidates are with `--list`, and it will refuse to choose for you.

M is the entire content of this correction. A script that discovered its own
family would decide, silently and by a filesystem glob, how large a multiplicity
it was correcting for -- and every judgement call in that glob (is a stub
campaign a look? a superseded one? a pilot?) would move the p-value with nothing
in the record saying so. §4 says L2 runs until **a human** stops it; the same
human declares what was in the family.

The list you pass is written into the verdict, so the family is recoverable.

## The validation null, and why it did not exist

`stage_nulls` builds a sign-flip null for every trial on the **train** split.
Nothing ever built one on validation, because nothing needed one until the L2
gate existed. It is the same machinery on a different fill log:
`round_turn.null_sample` over the blotter reconstructed from the validation run's
stored fills, on the validation session calendar.

Computed once per campaign and cached as `validation-null.json` in its directory.
Cached because it is deterministic in the fill log and the seed, and because
recomputing it on every L2 run would invite the thought that a different seed
might help.

## One tension this script does not resolve

§4 says validation is **not scored during the run at all** -- all M winners are
scored at the stop, at once, "to remove the temptation rather than relying on
discipline". Our practice differs: `nq_campaign.py --stage validate` scores a
campaign's winner immediately, and `nq-ml-03`'s was read the day it ran. §5 also
makes rule (a) a pass condition "at L1 AND L2", which needs a validation number
at L1.

So the codebase and §4 disagree about *when* validation is read, and this script
inherits whatever already happened rather than pretending otherwise. It is worth
Michael settling, because the protocol's stated defence against optional stopping
is exactly the part our practice has not followed. Recorded here, not decided
here.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Amendment 13: `runs/` unless a rehearsal redirects it. A rehearsal
# writes under `.scratch/`, which `runlog.ledger_paths()` does not read,
# so its trials are invisible to `global_n()` by construction.
import os as _os
_RUNS = pathlib.Path(_os.environ.get("PRACTICUM_RUNS_ROOT") or (ROOT / "runs"))
CAMPAIGNS = _RUNS / "campaigns"
VERDICT = _RUNS / "l2-verdict.json"

import prereg  # noqa: E402  -- scripts/ is on sys.path above


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(" ".join(text.split()), width)
B_NULL = 10_000


def cfg() -> dict:
    import yaml
    from src.core.costs import apply_costs
    return apply_costs(yaml.safe_load((ROOT / "config" / "run.yaml").read_text()))


def candidates() -> list[dict]:
    """Campaigns that *could* be in a family: a registered question, a winner
    scored on validation. Reported, never auto-selected."""
    out = []
    for d in sorted(p for p in CAMPAIGNS.iterdir() if p.is_dir()):
        val = d / "validation.json"
        rec = d / "campaign.json"
        if not (val.exists() and rec.exists()):
            continue
        v = json.loads(val.read_text())
        r = json.loads(rec.read_text()).get("record") or {}
        out.append({
            "campaign_id": d.name,
            "hypothesis": r.get("hypothesis_path"),
            "validation_profile": (v.get("validation") or {}).get("profile"),
            "validation_primary": (v.get("validation") or {}).get("primary"),
            "rule_a": v.get("rule_a_sortino_positive"),
            "superseded": (d / "SUPERSEDED.md").exists(),
            "has_null": (d / "validation-null.json").exists(),
        })
    return out


def build_validation_null(name: str, *, b: int = B_NULL) -> list[float]:
    """The sign-flip null of one winner's validation score. Cached per campaign.

    The same construction `stage_nulls` uses on train, on the validation fill
    log and the validation calendar. Seeded from the campaign name so it is
    reproducible and so it cannot be re-rolled by re-running.
    """
    d = CAMPAIGNS / name
    cache = d / "validation-null.json"
    if cache.exists():
        c = json.loads(cache.read_text())
        return c["sample"], c.get("rescored")

    from replay import load_fill_log
    from src.core.engine.reconstruct import reconstruct
    from src.core.scoring import metrics
    from src.core.scoring.randomization import round_turn as rt
    from src.core.types import Split
    import run as runner

    rows = [json.loads(x) for x in (d / "validation.jsonl").read_text().splitlines()
            if x.strip()]
    row = rows[-1]
    v = json.loads((d / "validation.json").read_text())
    profile_name = (v.get("validation") or {}).get("profile")
    profile = cfg()["profiles"][profile_name]

    fills = load_fill_log(row)
    if not fills:
        raise ValueError(f"{name}: the validation run left no fill log to resample")

    dataset = runner.build_dataset(cfg(), profile["dataset"], profile["seed"])
    sessions = dataset.scored_sessions(Split.VALIDATION,
                                       profile.get("warmup_sessions", 0))
    calendar = metrics.session_calendar(dataset, sessions)
    from src.core.costs import row_cost_terms
    blotter = reconstruct(fills,
                          commission_per_contract=profile["commission_per_contract"],
                          commission_bps=profile.get("commission_bps", 0.0),
                          **row_cost_terms(row))
    # **Seeded from the name with a STABLE hash.** `hash()` on a str is salted
    # per process (PYTHONHASHSEED), so `abs(hash(name))` would give this campaign
    # a different null on every run -- invisibly, because the cache below would
    # freeze whichever one happened first. Two runs must produce the same null,
    # and a family assembled in a different order must too.
    import hashlib
    seed = 90_000 + int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % 10_000

    # **The observed must be IN the null's support, as the no-flip draw.**
    # This gate rebuilds nulls *now* for validation runs scored *then*, so any
    # change to the metric between those two moments breaks comparability
    # silently. `nq-orderflow-01` is the instance: its validation score predates
    # the Amendment 3 idle charge reaching that path, its null was rebuilt with
    # today's scorer, and the two came out -0.1516 on Sharpe against -0.8455 on
    # Sortino -- a different objective and a different metric, reading as +17.8
    # standard deviations of skill. Re-deriving the identity assignment catches
    # every version skew of this shape, not only that one.
    # The same terms run.py scored the trial with (src/core/scoring/terms.py).
    from src.core.scoring.terms import scoring_terms
    terms = scoring_terms(cfg(), profile, dataset, calendar)
    # Amendment 17: read from its status line, so it is off until it is signed.
    attribution = rt.attribution_for(profile, prereg.in_force(17))
    ident = rt.identity_from_blotter(blotter, calendar, attribution=attribution, **terms)
    observed = (v.get("validation") or {}).get("score", {}).get("primary")
    obs_metric = (v.get("validation") or {}).get("score", {}).get("metric")
    try:
        rt.require_identity(observed, obs_metric, ident, attribution)
    except rt.NoFlipMismatch as exc:
        # SystemExit, because the caller's `except Exception` would otherwise drop
        # this trial from the family and carry on.
        raise SystemExit(f"{name}: {exc}") from exc

    # **The gate scores the observed in the same process as the null, from the
    # same fill log.** Using the number recorded at look-time against a null
    # rebuilt today is precisely the bug: `nq-orderflow-01`'s stored score
    # predates Amendment 3's idle charge reaching run.py, so it was -0.1516 on
    # Sharpe while every possible null draw was around -0.87 on Sortino.
    #
    # Recomputing is NOT a second look. Section 5's "once per campaign" is about
    # reading the split; a score is a pure function of the fill log, the calendar
    # and the scorer, and all three are already on disk. No market data is opened
    # here -- the same arithmetic, done once, consistently.
    rescored = None
    if observed is None or abs(float(observed) - ident.primary) > 1e-3 \
            or (obs_metric and obs_metric != ident.metric):
        rescored = {
            "stored_primary": observed, "stored_metric": obs_metric,
            "rescored_primary": ident.primary, "rescored_metric": ident.metric,
            "why": ("the stored validation score and this null disagree at the "
                    "no-flip assignment, which IS the observed under a sign-flip "
                    "null. The run was scored by an older version of the metric "
                    "than the one building its null -- check the run record for "
                    "`primary_uncharged`, whose absence dates it before "
                    "Amendment 5 and therefore before Amendment 3's idle charge "
                    "reached that path. The RESCORED value is used, because a "
                    "null and an observed computed under different rules cannot "
                    "be compared at all. The stored record is untouched."),
        }
        print(f"    !! {name}: stored {observed} on {obs_metric} -> rescored "
              f"{ident.primary:.6f} on {ident.metric}", flush=True)
    sample = rt.null_sample(blotter, calendar, b=b, seed=seed, attribution=attribution, **terms)
    cache.write_text(json.dumps(
        {"source": "round_turn_mc", "split": "validation", "b": b, "seed": seed,
         "profile": profile_name, "n_sessions": len(calendar),
         "fill_log_digest": row.get("fill_log_digest"),
         "spec": "docs/design/pre-registration.md sections 4 and 5",
         "rescored": rescored,
         "sample": sample}, indent=2))
    return sample, rescored


def validation_profitability(name: str, alpha: float) -> dict | None:
    """Amendment 18's bound on one winner's validation run, or None if it does not apply.

    Built from the same fill log, calendar and terms as `build_validation_null`.
    """
    from replay import load_fill_log
    from src.core.engine.reconstruct import reconstruct
    from src.core.scoring import metrics
    from src.core.scoring.profitability import applies, profitability_bound
    from src.core.scoring.terms import scoring_terms
    from src.core.types import Split
    import run as runner

    d = CAMPAIGNS / name
    v = json.loads((d / "validation.json").read_text())
    profile = cfg()["profiles"][(v.get("validation") or {}).get("profile")]
    if not applies(profile, prereg.in_force(18)):
        return None
    rows = [json.loads(x) for x in (d / "validation.jsonl").read_text().splitlines()
            if x.strip()]
    fills = load_fill_log(rows[-1])
    if not fills:
        raise SystemExit(f"{name}: the validation run left no fill log for Amendment 18")
    dataset = runner.build_dataset(cfg(), profile["dataset"], profile["seed"])
    sessions = dataset.scored_sessions(Split.VALIDATION, profile.get("warmup_sessions", 0))
    calendar = metrics.session_calendar(dataset, sessions)
    from src.core.costs import row_cost_terms
    blotter = reconstruct(fills, commission_per_contract=profile["commission_per_contract"],
                          commission_bps=profile.get("commission_bps", 0.0),
                          **row_cost_terms(rows[-1]))
    terms = scoring_terms(cfg(), profile, dataset, calendar)
    return profitability_bound(metrics.session_returns(blotter, calendar, **terms), alpha)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                    help="show the campaigns that could be in a family, and stop")
    ap.add_argument("--campaigns",
                    help="comma-separated campaign ids -- the declared family")
    ap.add_argument("--q", type=float, default=0.05)
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print; write no verdict file")
    a = ap.parse_args()

    cands = candidates()
    if a.list or not a.campaigns:
        print(f"{len(cands)} campaign(s) have a winner scored on validation:\n")
        print(f"  {'campaign':28} {'rule (a)':9} {'null':6} {'superseded':11} hypothesis")
        for c in cands:
            print(f"  {c['campaign_id']:28} {str(c['rule_a']):9} "
                  f"{'yes' if c['has_null'] else 'no':6} "
                  f"{'YES' if c['superseded'] else '-':11} "
                  f"{(c['hypothesis'] or '(none)').replace('config/hypotheses/', '')}")
        print("\nM is declared, not discovered: pass --campaigns a,b,c.")
        print("Every campaign you ran belongs in the family -- including the ones "
              "that did badly.\nDropping them shrinks M and inflates the maximum's "
              "significance, which is\nthe same defect as excluding crashed trials "
              "from N.")
        if not a.campaigns:
            return 0

    family = [x.strip() for x in a.campaigns.split(",") if x.strip()]
    known = {c["campaign_id"] for c in cands}
    missing = [f for f in family if f not in known]
    if missing:
        sys.exit(f"not scored on validation, so cannot be in a family: "
                 f"{', '.join(missing)}")

    print(f"declared family: M = {len(family)}\n")
    winners, t0 = [], time.monotonic()
    for name in family:
        c = next(x for x in cands if x["campaign_id"] == name)
        try:
            sample, rescored = build_validation_null(name)
            if rescored:
                c["validation_primary"] = rescored["rescored_primary"]
                c["rescored"] = rescored
        except Exception as exc:                                 # noqa: BLE001
            print(f"  {name:28} NULL FAILED  {type(exc).__name__}: {exc}"[:118])
            sample = []
        print(f"  {name:28} validation {c['validation_primary']:+.4f}  "
              f"{len(sample):>6} null draws  {time.monotonic() - t0:.0f}s",
              flush=True)
        winner = {"campaign_id": name,
                  "validation_primary": c["validation_primary"],
                  "validation_null": sample}
        # Amendment 18: q / M, with M the declared family.
        bound = validation_profitability(name, a.q / len(family))
        if bound is not None:
            winner["profitability_pass"] = bound["passes"]
            print(f"  {name:28} profitability lower bound {bound['lower_bound']} "
                  f"at alpha {bound['alpha']:g}: {bound['passes']}", flush=True)
        winners.append(winner)

    from src.core.scoring.correction import max_of_m
    # Which statistic decides is read from the pre-registration, never from a
    # flag here: Amendment 10's status line IS the switch, so the signature and
    # the behaviour cannot drift apart.
    statistic = "standardised" if prereg.in_force(10) else "raw"
    print(f"\n  statistic: {statistic}  ({prereg.status(10)})")
    out = max_of_m(winners, b=B_NULL, q=a.q, statistic=statistic)

    print(f"\n  M                {out['m_campaigns']}")
    print(f"  best campaign    {out.get('best_campaign')}")
    print(f"  observed best    {out.get('observed_best')}")
    if out.get("verdict") is None:
        print(f"  VERDICT WITHHELD {out.get('withheld') or out.get('note')}")
    else:
        print(f"  null max mean    {out['null_max_mean']:+.4f}")
        print(f"  p                {out['p']:.4f}   (q = {a.q})")
        print(f"  rule (a) Sortino > 0 on the best   {out['rule_a_pass']}")
        print(f"  rule (b) max_of_m p <= q           {out['reject']}")
        print(f"  VERDICT          {out['verdict']}")

        # Task 70: each campaign's distance above its OWN null. The raw maximum
        # cannot see this, and it is the whole reason the two statistics differ.
        print("\n  each campaign against its own validation null:")
        for c in out.get("per_campaign") or ():
            z = c.get("sds_above_own_null")
            zs = "    n/a" if z is None else f"{z:+7.2f}"
            print(f"    {c['campaign_id']:24} obs {c['validation_primary']:+.4f}  "
                  f"null {c['null_mean']:+.4f} +/- {c['null_sd']:.4f}  {zs} sd")
        # Both views, each labelled by whether it is the one deciding. Printing
        # one of them as "DIAGNOSTIC" unconditionally was wrong the moment
        # Amendment 10 made the standardised statistic the verdict.
        sv, rv = out.get("standardised_view") or {}, out.get("raw_view") or {}
        live = out.get("statistic")
        if sv:
            tag = "IN FORCE" if live == "standardised" else "diagnostic"
            print(f"\n  standardised statistic [{tag}]: best "
                  f"{sv.get('best_campaign')} at "
                  f"{sv.get('observed_best_sds'):+.2f} sd, p = {sv.get('p'):.4f}, "
                  f"reject = {sv.get('reject')}")
        if rv:
            tag = "IN FORCE" if live == "raw" else "diagnostic"
            print(f"  raw statistic          [{tag}]: best "
                  f"{rv.get('best_campaign')} at "
                  f"{rv.get('observed_best'):+.4f} Sortino, p = {rv.get('p'):.4f}, "
                  f"reject = {rv.get('reject')}")
        if out.get("disagreement"):
            print("\n  !! THE TWO STATISTICS DISAGREE")
            for line in _wrap(out["disagreement"], 74):
                print(f"     {line}")
    print("\n  Not a promotion. Section 5 is a pass rule; promotion beyond the "
          "MVP is a\n  human decision on all of the evidence.")

    if a.dry_run:
        print("\n  dry run: no verdict file written")
        return 0
    out["schema"] = "l2-verdict/1"
    out["declared_family"] = family
    VERDICT.write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(f"\n  wrote {VERDICT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
