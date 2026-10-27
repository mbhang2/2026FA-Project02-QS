"""**The POWER measurement: does a null ACCEPT an edge that is known real?**

Every other FWER rig in this repo measures false positives -- how often a
no-edge candidate is wrongly accepted. None of them measures the other error.
A null that rejects everything has perfect FWER and is useless.

spx-arb-01 is the only campaign where the edge is known genuine by
construction: a planted no-arbitrage violation worth ~7.19 points net of
spread, which the engine was separately proved to realise exactly (+0.45
theoretical = +0.45 measured on a box spread). The gate REJECTED it at
p = 0.0993. So it is the one available power test, and it needs no gateway:
the 50 trials are already run, scored and stored, and only the null is
rebuilt.

**What this script separates, and what the first pass confounded.** Changing
the NULL (sign-flip -> random trader) and changing the RULE (independent
resampling -> aligned/CRN) are different interventions. Run together they
produced p = 0.0001 and looked like a triumph; the 0.0001 was almost entirely
the rule, and the rule was wrong.

    null                                 independent      aligned
    sign-flip (per trial, 50 distinct)        0.0993       0.0985
    random trader, ONE shared table           0.0001       0.0001
    random trader, 11 bucket tables           0.0026       0.0003

    family p as trials are added, winner always included:
      N    signflip/indep   rt-bucket/indep   rt-shared/aligned
      1            0.0005            0.0001              0.0001
     10            0.0089            0.0001              0.0001
     20            0.0167            0.0001              0.0001
     35            0.0663            0.0009              0.0001
     50            0.0934            0.0016              0.0001

**Three readings.**

1. The random-trader null is genuinely more powerful, with the correction
   fully ON: 0.0993 REJECT -> 0.0026 ACCEPT. The winner was individually
   significant under both nulls (p1 = 0.0004 sign-flip, 0.0001 bucketed), so
   the whole difference is the max-of-N bar. Under sign-flip that bar is set
   by trials with unstable Sortino denominators -- the -0.775 correlation --
   and a shared marginal removes that channel.

2. `aligned` over shared tables is NOT a dependence correction, it is the
   absence of one. Its column is flat in N: 0.0001 at N=1 and at N=50.
   Multiplicity does not disappear because the per-trial null was computed
   once; reusing a table saves compute, not error rate. Registration refuses
   the combination.

3. p SHOULD rise with N, and it does under both calibrated configurations --
   but the STEEPNESS was an artefact of per-trial nulls. Sign-flip inflates
   187x from N=1 to N=50; shared bucket tables inflate 16x. That is the
   legitimate version of "N should not dominate the p-value."

**THE 0.0026 IS INDICATIVE, NOT LICENSED, AND THE REASON MATTERS.**

19 of spx-arb-01's 50 scored trials trade below 1.0 round turns a session.
`stage_nulls` therefore REFUSES this campaign outright, and the 0.0026 above
was computed with five bucket tables (0.345, 0.449, 0.584, 0.759, 0.986)
sitting in the regime whose matched-bucket FWER measures 0.082 -- above q.
The winner itself is at 1.438 and licensed; the family it belongs to is not,
and a family p needs a null for every member.

So the power finding and the activity floor are in direct tension on the one
campaign where the edge is known real. That is not a reason to lower the
floor to fit the example -- it is the clearest statement of the null's actual
standing: more powerful where it is licensed, and not licensed where the
interesting campaigns live. nq-orderflow-absorption-02's 388 scored trials
have a MEDIAN activity of 0.138, so it is refused far harder than this one.

**Caveat that limits every number above.** The random-trader null has
narrower support: a coin-flipping trader never reaches Sortino 5.74 on this
data, so one shared table hits the 1/(b+1) resolution floor. Part of its
power is that it is a WEAKER adversary than sign-flip, not only a
better-behaved one. And the 0.042 FWER was measured on NQ at a single matched
bucket, not on this 11-bucket SPX spread, so it does not transfer unchecked.

Usage:
    .venv/bin/python scripts/null_power.py /tmp/out.json
"""
import json, pathlib, sys, time
import numpy as np

ROOT = pathlib.Path("/Users/minhyukbhang/Desktop/Practicum/2026FA-Project02-2-QS")
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
from src.core.scoring.correction import max_of_n
from src.core.scoring.randomization import random_trader as rtr
from src.core.loop.campaign import CampaignRecord
from src.core.scoring import metrics
from src.core.types import Split
import run as runner, yaml

d = ROOT / "runs/campaigns/spx-arb-01"
rows = [json.loads(x) for x in (d/"ledger.jsonl").read_text().splitlines() if x.strip()]
rec, _ = CampaignRecord.read(d/"campaign.json")
cfg = yaml.safe_load((ROOT/"config/run.yaml").read_text())
profile = cfg["profiles"][rec.profile]
comm = profile["commission_per_contract"]
scored = [r for r in rows if (r.get("score") or {}).get("primary") is not None]
obs = [r["score"]["primary"] for r in scored]
best = max(obs); N = len(scored)
act = [(r["score"].get("trade_count") or 0) / max(r["score"].get("n_sessions") or 1, 1)
       for r in scored]
stored = json.loads((d/"nulls.json").read_text())

dataset = runner.build_dataset(cfg, profile["dataset"], profile["seed"])
by = {}
for tick in dataset.ticks(Split.TRAIN):
    by.setdefault(tick.session, []).append(tick.price)
days = [v for _, v in sorted(by.items())]
ITERS = max(10_000, rtr.MIN_DRAWS_PER_TRIAL * N)

t0 = time.monotonic()
tabs = {}
for b_i in sorted({rtr.bucket_index(a) for a in act}):
    sp = rtr.spec_for_activity(rtr.bucket_activity(b_i), iterations=ITERS,
                               commission_per_contract=comm)
    tabs[b_i] = rtr.build_table(days, sp, dataset=dataset.name, split="train")
print(f"{len(tabs)} bucket tables in {time.monotonic()-t0:.0f}s", flush=True)

wa = act[int(np.argmax(obs))]
tA = tabs[rtr.bucket_index(wa)]
nulls = {
    "sign-flip (per trial, 50 distinct)": stored["samples"],
    "random trader, ONE shared table":   [tA.draws] * N,
    "random trader, 11 bucket tables":   [tabs[rtr.bucket_index(a)].draws for a in act],
}
print(f"\n{'null':<36} {'independent':>12} {'aligned':>10}")
print(f"{'':<36} {'(calibrated)':>12} {'(degenerate)':>10}")
out = {}
for name, samp in nulls.items():
    pi = max_of_n(samp, best, b=10_000)["p"]
    pa = max_of_n(samp, best, b=10_000, aligned=True)["p"]
    out[name] = {"independent": pi, "aligned": pa}
    print(f"{name:<36} {pi:>12.4f} {pa:>10.4f}", flush=True)

# does p still depend on N under each combination?
print(f"\nthe N question: family p as trials are added (winner always included)")
order = sorted(range(N), key=lambda i: -obs[i])     # winner first
print(f"  {'N':>4}  {'signflip/indep':>15} {'rt-bucket/indep':>16} {'rt-shared/aligned':>18}")
for n in (1, 5, 10, 20, 35, 50):
    keep = order[:n]
    a1 = max_of_n([stored["samples"][i] for i in keep], best, b=10_000)["p"]
    a2 = max_of_n([tabs[rtr.bucket_index(act[i])].draws for i in keep], best, b=10_000)["p"]
    a3 = max_of_n([tA.draws] * n, best, b=10_000, aligned=True)["p"]
    print(f"  {n:>4}  {a1:>15.4f} {a2:>16.4f} {a3:>18.4f}", flush=True)
    out.setdefault("byN", {})[n] = [a1, a2, a3]
json.dump(out, open(sys.argv[1], "w"), indent=2)
print("\ndone", flush=True)
