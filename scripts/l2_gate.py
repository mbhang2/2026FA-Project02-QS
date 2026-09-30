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

CAMPAIGNS = ROOT / "runs" / "campaigns"
VERDICT = ROOT / "runs" / "l2-verdict.json"
B_NULL = 10_000


def cfg() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "config" / "run.yaml").read_text())


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
        return json.loads(cache.read_text())["sample"]

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
    blotter = reconstruct(fills,
                          commission_per_contract=profile["commission_per_contract"])
    # **Seeded from the name with a STABLE hash.** `hash()` on a str is salted
    # per process (PYTHONHASHSEED), so `abs(hash(name))` would give this campaign
    # a different null on every run -- invisibly, because the cache below would
    # freeze whichever one happened first. Two runs must produce the same null,
    # and a family assembled in a different order must too.
    import hashlib
    seed = 90_000 + int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % 10_000
    sample = rt.null_sample(blotter, calendar, b=b, seed=seed)
    cache.write_text(json.dumps(
        {"source": "round_turn_mc", "split": "validation", "b": b, "seed": seed,
         "profile": profile_name, "n_sessions": len(calendar),
         "fill_log_digest": row.get("fill_log_digest"),
         "spec": "docs/design/pre-registration.md sections 4 and 5",
         "sample": sample}, indent=2))
    return sample


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
            sample = build_validation_null(name)
        except Exception as exc:                                 # noqa: BLE001
            print(f"  {name:28} NULL FAILED  {type(exc).__name__}: {exc}"[:118])
            sample = []
        print(f"  {name:28} validation {c['validation_primary']:+.4f}  "
              f"{len(sample):>6} null draws  {time.monotonic() - t0:.0f}s",
              flush=True)
        winners.append({"campaign_id": name,
                        "validation_primary": c["validation_primary"],
                        "validation_null": sample})

    from src.core.scoring.correction import max_of_m
    out = max_of_m(winners, b=B_NULL, q=a.q)

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
