#!/usr/bin/env python3
"""Task 38: the hybrid's FWER measured at R = 150, the resolution we operate at.

    python docs/reviews/task38/hybrid_fwer_r150.py --stage verify
    python docs/reviews/task38/hybrid_fwer_r150.py --stage extend
    python docs/reviews/task38/hybrid_fwer_r150.py --stage fresh
    python docs/reviews/task38/hybrid_fwer_r150.py --stage report

Task 33 measured the hybrid null's family-wise error rate at **0.058, CI
[0.039, 0.085]**, with the rotation fallback at **R = 50** -- chosen because
R = 150 was not affordable that day. Task 34 then measured the correction's own
resampling bias as a function of R (+0.011 at R = 50, +0.002 at R = 150) and
restated the headline as "0.058 -> about 0.047 **by correction, not by
construction**", explicitly noting that the by-construction number needed a
re-run nobody had done.

This is that re-run. **Nothing is tuned in response to the result**: same
conditional framing, same hybrid routing rule, same calibration pool, same
candidate sources, same q, same N per campaign. The only thing that changes is
the number of rotations behind each fallback null.

## Why this is cheaper and sharper than re-running Task 33

Task 33's `campaigns.json` holds its 4,000 base runs, their routing decisions,
and the 883 candidates that fell back to rotation. Those are all deterministic
functions of the candidate source and the calibration table, so the first 400
campaigns do not need to be re-run -- only **100 more rotations per fallback
candidate**, seeded past the 50 that already exist.

That makes the comparison **paired**: identical candidates, identical observed
scores, identical routing. The difference between the two headline numbers is
attributable to R and to nothing else, and it can be tested on the campaigns
that flipped rather than by comparing two confidence intervals.

`--stage verify` re-runs a sample of Task 33's base candidates and asserts the
scores reproduce, because the pairing is only worth anything if the
regenerated sources are the same sources.

## The pre-registration, written before any campaign ran

**C = 400 campaigns x N = 10 trials, fallback R = 150, q = 0.05.** Campaigns
0-399 are Task 33's, extended from R = 50 to R = 150, and nothing else changes.

C was chosen against a measured throughput rather than a guess. The brief asked
for a larger C if one was affordable overnight, so the rate was measured first:
**3.8 isolated runs/s on 9 workers, 400 runs in 104s** -- which agrees with the
3.6/s Task 34 reported and disagrees with the 7.7/s Task 36 saw on its
campaign nulls. At that rate:

| | runs | wall clock |
|---|---:|---:|
| extend campaigns 0-399 to R = 150 | 88,300 | **6.4 h** |
| add fresh campaigns 400-799 | 136,450 | 9.9 h |

So C = 800 is 16 hours, not an overnight, and it is not what this runs.

**What C = 400 resolves**, at a true rate near 0.05: standard error **0.0109**,
and a Wilson 95% interval of roughly **[0.032, 0.077]**. It separates 0.05 from
0.09; it does not separate 0.05 from 0.07. **What C = 800 would have bought**:
se 0.0077 and an interval near [0.037, 0.068] -- about a 25% narrower interval
for 2.5x the compute, with the second 400 campaigns unpaired.

**The pairing is what makes C = 400 enough here, and it is the reason this is
not simply Task 33 run again.** The two estimates share their candidates, their
observed scores and their routing, so the R = 50 versus R = 150 contrast is a
within-campaign comparison: it is tested on the campaigns that *flipped*
(McNemar, exact, two sided) rather than by asking whether two intervals overlap.
Comparing two independent C = 400 intervals could not resolve a shift of 0.011
at all; the paired test can.

`--stage fresh` implements the extension to C = 800 and is deliberately left
unrun. Reporting it later would be a campaign count chosen after seeing a
result, which is the failure this project exists to prevent.

Two results are reported, both specified here: the **paired** R = 50 vs R = 150
contrast, and the R = 150 rate with its interval, which is the headline.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
HERE = pathlib.Path(__file__).resolve().parent
T33 = REPO / "docs" / "reviews" / "task33"
WORK = REPO / "runs" / "tmp-task38"

# Task 33's constants. Not parameters -- if any of these moves, the comparison
# is between two different experiments.
DATASET_SEED = 4_242
N_TRIALS = 10
Q = 0.05
SOURCE_SEED = 33_002
ROT_SEED_BASE = 800_000
T33_CAMPAIGNS = 400
T33_R = 50

# Task 38's own.
CAMPAIGNS = 400          # the pre-registration; `fresh` would make it 800
FALLBACK_R = 150
WORKERS = 8              # one core left for whatever else is running
VERIFY_SAMPLE = 24


def _run(args):
    """One isolated run. Top level so it can cross a process pool.

    Byte-for-byte the same construction as `docs/reviews/task33/hybrid_fwer.py`.
    """
    path, rot_seed, signal = args
    sys.path.insert(0, str(REPO))
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.engine import Engine
    from src.core.engine.remote import RemoteStrategy
    from src.core.runlog import RunTicket
    from src.core.scoring import metrics as metricsmod
    from src.core.scoring.randomization.rotation import RotatedDataset
    from src.core.scoring.scorer import score
    from src.core.types import Split

    ds = SyntheticDataset(SyntheticSpec(
        n_sessions=175, ticks_per_session=390, vol_per_tick=0.15,
        seed=DATASET_SEED, signal_strength=signal))
    if rot_seed is not None:
        ds = RotatedDataset(ds, seed=rot_seed)
    ticket = RunTicket.for_null_calibration("task 38 hybrid calibration at R=150")
    with RemoteStrategy(pathlib.Path(path), "Strategy") as strategy:
        engine = Engine(ds, strategy, ticket=ticket, split=Split.TRAIN,
                        warmup_sessions=0, fill_model="unfavorable", seed=1)
        engine.run()
    blotter = engine.reconstructed()
    period = engine.session_calendar()
    s = score(blotter, period=period)
    return {"score": None if s.rejected else s.primary,
            "returns": metricsmod.session_returns(blotter, period)}


def sources(n: int) -> list[str]:
    """Task 33's candidate stream, continued.

    `NullGenerator(seed=33_002)` is deterministic, so indices 0-3999 regenerate
    Task 33's candidates exactly and 4000+ are the fresh ones. One stream rather
    than two seeds, so "fresh" cannot accidentally mean "the same again".
    """
    from src.core.loop.agents import NullGenerator

    WORK.mkdir(parents=True, exist_ok=True)
    agent = NullGenerator(seed=SOURCE_SEED)
    out = []
    for i in range(n):
        f = WORK / f"camp{i}.py"
        f.write_text(agent.propose([]))
        out.append(str(f))
    return out


def parallel(jobs, label):
    t0 = time.monotonic()
    out = []
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for i, r in enumerate(pool.map(_run, jobs, chunksize=4), start=1):
            out.append(r)
            if i % 2_000 == 0:
                rate = i / (time.monotonic() - t0)
                print(f"  {label}: {i:,}/{len(jobs):,}  {rate:.1f}/s  "
                      f"eta {(len(jobs) - i) / rate / 60:.0f} min", flush=True)
    print(f"  {label}: {len(jobs):,} in {time.monotonic() - t0:.0f}s "
          f"({len(jobs) / max(time.monotonic() - t0, 1e-9):.1f}/s)", flush=True)
    return out


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


# --------------------------------------------------------------------------- #
# verify: are these the same candidates Task 33 ran?
# --------------------------------------------------------------------------- #
def stage_verify() -> None:
    doc = json.loads((T33 / "campaigns.json").read_text())
    paths = sources(T33_CAMPAIGNS * N_TRIALS)
    step = max(1, len(paths) // VERIFY_SAMPLE)
    idx = list(range(0, len(paths), step))[:VERIFY_SAMPLE]
    t0 = time.monotonic()
    out = parallel([(paths[i], None, 0.0) for i in idx], "verify")
    rate = len(idx) / (time.monotonic() - t0)

    bad = [(i, doc["base"][i]["score"], r["score"])
           for i, r in zip(idx, out) if r["score"] != doc["base"][i]["score"]]
    print(f"\nregenerated {len(paths):,} sources from NullGenerator(seed="
          f"{SOURCE_SEED}); re-ran {len(idx)} of them")
    print(f"  reproduced {len(idx) - len(bad)}/{len(idx)} of Task 33's base "
          f"scores exactly")
    for i, a, b in bad[:5]:
        print(f"    candidate {i}: task33 {a!r}, now {b!r}")
    print(f"  measured throughput {rate:.1f} isolated runs/s on {WORKERS} workers")
    fallbacks = len(doc["fallback_index"])
    extend = fallbacks * (FALLBACK_R - T33_R)
    print(f"\ncost, at that rate:")
    print(f"  extend  {fallbacks:,} fallback candidates x "
          f"{FALLBACK_R - T33_R} more rotations = {extend:,} runs "
          f"= {extend / rate / 3600:.1f} h")
    fresh_base = (CAMPAIGNS - T33_CAMPAIGNS) * N_TRIALS
    fresh_rot = int(fresh_base * (fallbacks / (T33_CAMPAIGNS * N_TRIALS))
                    * FALLBACK_R)
    print(f"  fresh   {fresh_base:,} base + about {fresh_rot:,} rotations "
          f"= {(fresh_base + fresh_rot) / rate / 3600:.1f} h")
    print(f"  total   about {(extend + fresh_base + fresh_rot) / rate / 3600:.1f} h")
    if bad:
        sys.exit("\nthe regenerated candidates are NOT Task 33's. The paired "
                 "comparison is off the table; re-run the whole calibration or "
                 "abandon the pairing explicitly.")


# --------------------------------------------------------------------------- #
# extend: Task 33's 400 campaigns, from R = 50 to R = 150
# --------------------------------------------------------------------------- #
def stage_extend() -> None:
    if (HERE / "extend.json").exists():
        print("extend.json exists, skipping")
        return
    doc = json.loads((T33 / "campaigns.json").read_text())
    assert doc["fallback_r"] == T33_R, doc["fallback_r"]
    paths = sources(T33_CAMPAIGNS * N_TRIALS)
    fallback = doc["fallback_index"]
    # Seeded past Task 33's k = 0..49 on the same 1,000-wide per-candidate
    # block, so the existing 50 rotations stay valid and the new 100 are drawn
    # from offsets nothing has used.
    jobs = [(paths[i], ROT_SEED_BASE + 1_000 * i + k, 0.0)
            for i in fallback for k in range(T33_R, FALLBACK_R)]
    print(f"extending {len(fallback):,} fallback nulls from R={T33_R} to "
          f"R={FALLBACK_R}: {len(jobs):,} runs", flush=True)
    rot = parallel(jobs, "extend")
    (HERE / "extend.json").write_text(json.dumps({
        "fallback_index": fallback, "added_per_candidate": FALLBACK_R - T33_R,
        "rotation": [r["score"] for r in rot]}))
    print(f"wrote {HERE / 'extend.json'}")


# --------------------------------------------------------------------------- #
# fresh: campaigns 400-799
# --------------------------------------------------------------------------- #
def stage_fresh() -> None:
    if (HERE / "fresh.json").exists():
        print("fresh.json exists, skipping")
        return
    from src.core.scoring.randomization import choose_null

    table = profile_table()
    paths = sources(CAMPAIGNS * N_TRIALS)
    lo = T33_CAMPAIGNS * N_TRIALS
    fresh_paths = paths[lo:]
    print(f"fresh campaigns {T33_CAMPAIGNS}-{CAMPAIGNS - 1}: "
          f"{len(fresh_paths):,} base runs", flush=True)
    base = parallel([(p, None, 0.0) for p in fresh_paths], "fresh-base")

    routes = [choose_null(r["returns"], table) if r["score"] is not None else None
              for r in base]
    fallback = [i for i, c in enumerate(routes) if c and c.route == "rotation"]
    print(f"  rotation share {len(fallback):,}/{len(base):,} = "
          f"{len(fallback) / len(base):.1%}  -> "
          f"{len(fallback) * FALLBACK_R:,} runs", flush=True)
    jobs = [(fresh_paths[i], ROT_SEED_BASE + 1_000 * (lo + i) + k, 0.0)
            for i in fallback for k in range(FALLBACK_R)]
    rot = parallel(jobs, "fresh-rotation")
    (HERE / "fresh.json").write_text(json.dumps({
        "offset": lo,
        "base": [{"score": r["score"], "returns": r["returns"]} for r in base],
        "routes": [c.as_dict() if c else None for c in routes],
        "fallback_index": fallback,
        "rotation": [r["score"] for r in rot],
        "fallback_r": FALLBACK_R}))
    print(f"wrote {HERE / 'fresh.json'}")


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def profile_table():
    """Task 33's calibration table, rebuilt from its committed pool.

    **`pool.json` no longer carries the return series it was written with.** It
    was compacted after Task 33's report ran -- each row is now
    `{"score", "bucket"}` rather than `{"score", "returns"}` -- so
    `ProfileNull.from_runs`, which is what Task 33 called, raises `KeyError` on
    the committed artifact. `docs/reviews/task33/hybrid_fwer.py --stage report`
    is therefore no longer re-runnable as committed, which is worth writing down
    rather than quietly working around.

    The compacted form loses nothing this needs: `from_runs` only ever used the
    returns to compute `bucket_of(...)`, and the bucket is what was kept. So the
    table is rebuilt by grouping on the stored bucket, and the two constructions
    agree by definition. The `from_runs` branch stays for a pool written the old
    way.
    """
    from src.core.scoring.randomization.profile_null import ProfileNull

    pool = json.loads((T33 / "pool.json").read_text())
    if pool and "returns" in pool[0]:
        return ProfileNull.from_runs([(r["returns"], r["score"]) for r in pool])
    buckets: dict[str, list[float]] = {}
    for r in pool:
        if r.get("score") is None:
            continue
        buckets.setdefault(r["bucket"], []).append(float(r["score"]))
    return ProfileNull(buckets=buckets)


def _campaign_rejects(base, routes, nulls_by_index, table, lo, hi, r_needed):
    """Which campaigns in [lo, hi) produce a discovery. Task 33's rule, verbatim
    except that `r_needed` is the R this pass ran at."""
    from src.core.scoring.correction import max_of_n

    out, used_rot, used_bucket, missing = [], 0, 0, 0
    for c in range(lo, hi):
        samples, observed = [], []
        for j in range(c * N_TRIALS, (c + 1) * N_TRIALS):
            route = routes[j]
            if route is None or base[j]["score"] is None:
                missing += 1
                continue
            if route["route"] == "bucket":
                samples.append(table.buckets[route["bucket"]])
                used_bucket += 1
            else:
                sample = nulls_by_index.get(j, [])
                if len(sample) < r_needed // 2:
                    missing += 1
                    continue
                samples.append(sample)
                used_rot += 1
            observed.append(base[j]["score"])
        out.append(bool(samples) and
                   max_of_n(samples, max(observed), b=10_000, q=Q)["reject"])
    return out, used_bucket, used_rot, missing


def stage_report() -> None:
    from src.core.scoring.correction import resolution_cap

    table = profile_table()
    t33 = json.loads((T33 / "campaigns.json").read_text())
    ext = json.loads((HERE / "extend.json").read_text())

    base, routes = list(t33["base"]), list(t33["routes"])
    r50, r150 = {}, {}
    for slot, i in enumerate(t33["fallback_index"]):
        chunk = t33["rotation"][slot * T33_R:(slot + 1) * T33_R]
        r50[i] = [x for x in chunk if x is not None]
    add = ext["added_per_candidate"]
    for slot, i in enumerate(ext["fallback_index"]):
        chunk = ext["rotation"][slot * add:(slot + 1) * add]
        r150[i] = r50[i] + [x for x in chunk if x is not None]

    at50, b50, rot50, miss50 = _campaign_rejects(
        base, routes, r50, table, 0, T33_CAMPAIGNS, T33_R)
    at150, b150, rot150, miss150 = _campaign_rejects(
        base, routes, r150, table, 0, T33_CAMPAIGNS, FALLBACK_R)

    pooled = list(at150)
    fresh_stats = None
    fresh_path = HERE / "fresh.json"
    if fresh_path.exists():
        fr = json.loads(fresh_path.read_text())
        fbase = list(fr["base"])
        froutes = list(fr["routes"])
        fnull = {}
        for slot, i in enumerate(fr["fallback_index"]):
            chunk = fr["rotation"][slot * FALLBACK_R:(slot + 1) * FALLBACK_R]
            fnull[i] = [x for x in chunk if x is not None]
        n_fresh = len(fbase) // N_TRIALS
        fres, fb, frot, fmiss = _campaign_rejects(
            fbase, froutes, fnull, table, 0, n_fresh, FALLBACK_R)
        pooled += fres
        fresh_stats = {"campaigns": n_fresh, "rejects": sum(fres),
                       "bucket": fb, "rotation": frot, "missing": fmiss}

    # paired contrast on the shared 400
    both = sum(1 for a, b in zip(at50, at150) if a and b)
    only50 = sum(1 for a, b in zip(at50, at150) if a and not b)
    only150 = sum(1 for a, b in zip(at50, at150) if b and not a)
    disc = only50 + only150
    # exact binomial, two sided, on the discordant pairs (McNemar)
    p_mcnemar = (1.0 if disc == 0 else min(1.0, 2 * sum(
        math.comb(disc, k) for k in range(min(only50, only150) + 1)) / 2 ** disc))

    lo50, hi50 = wilson(sum(at50), len(at50))
    lo150, hi150 = wilson(sum(at150), len(at150))
    lop, hip = wilson(sum(pooled), len(pooled))
    cap = resolution_cap(FALLBACK_R, Q)
    total = b150 + rot150 + miss150

    L = []
    L.append(f"hybrid null, q = {Q}, N = {N_TRIALS} trials per campaign")
    L.append("")
    L.append("PAIRED -- the same 400 campaigns, the same candidates, the same")
    L.append("observed scores, the same routing. Only R changes.")
    L.append(f"  R =  {T33_R:<4} FWER {sum(at50) / len(at50):.4f}  "
             f"95% CI [{lo50:.3f}, {hi50:.3f}]   ({sum(at50)}/{len(at50)})")
    L.append(f"  R = {FALLBACK_R:<4} FWER {sum(at150) / len(at150):.4f}  "
             f"95% CI [{lo150:.3f}, {hi150:.3f}]   ({sum(at150)}/{len(at150)})")
    L.append(f"  both reject {both}   only at R={T33_R} {only50}   "
             f"only at R={FALLBACK_R} {only150}")
    L.append(f"  McNemar exact on {disc} discordant campaigns: p = {p_mcnemar:.4f}")
    L.append("")
    if fresh_stats:
        L.append(f"POOLED -- C = {len(pooled)} campaigns at R = {FALLBACK_R}")
        L.append(f"  FWER {sum(pooled) / len(pooled):.4f}  "
                 f"95% CI [{lop:.3f}, {hip:.3f}]   "
                 f"({sum(pooled)}/{len(pooled)})")
    else:
        L.append(f"POOLED -- `--stage fresh` deliberately not run. The headline is")
        L.append(f"the R = {FALLBACK_R} figure above, at the pre-registered "
                 f"C = {T33_CAMPAIGNS}.")
    L.append("")
    L.append(f"ROUTING at R = {FALLBACK_R}, campaigns 0-{T33_CAMPAIGNS - 1}")
    L.append(f"  bucket {b150:,}/{total:,} = {b150 / total:.1%}   "
             f"rotation {rot150:,}/{total:,} = {rot150 / total:.1%}   "
             f"no null {miss150:,} = {miss150 / total:.1%}")
    if fresh_stats:
        ft = fresh_stats["bucket"] + fresh_stats["rotation"] + fresh_stats["missing"]
        L.append(f"  fresh campaigns: bucket {fresh_stats['bucket'] / ft:.1%}   "
                 f"rotation {fresh_stats['rotation'] / ft:.1%}   "
                 f"no null {fresh_stats['missing'] / ft:.1%}")
    L.append("")
    L.append(f"RESOLUTION CAP at R = {FALLBACK_R}")
    L.append(f"  resampling_bias {cap['resampling_bias']}   "
             f"meets_recommended_r {cap['meets_recommended_r']}   "
             f"p_floor {cap['p_floor']:.4f}   bh_max_n {cap['bh_max_n']}")
    L.append("")
    L.append("FOR COMPARISON")
    L.append("  Task 33, R = 50, C = 400:   0.0575  [0.039, 0.085]")
    L.append("  Task 34, corrected by bias(R), not re-run:  about 0.047")
    L.append("  Task 32, profile only, 21.8% auto-rejected: 0.045 [0.029, 0.070]")
    text = "\n".join(L)
    print(text)

    (HERE / "fwer-r150.json").write_text(json.dumps({
        "q": Q, "n_trials": N_TRIALS, "fallback_r": FALLBACK_R,
        "paired": {
            "campaigns": len(at50),
            "r50": {"rejects": sum(at50), "fwer": sum(at50) / len(at50),
                    "ci": [lo50, hi50]},
            "r150": {"rejects": sum(at150), "fwer": sum(at150) / len(at150),
                     "ci": [lo150, hi150]},
            "both": both, "only_r50": only50, "only_r150": only150,
            "mcnemar_exact_p": p_mcnemar,
        },
        "pooled": {"campaigns": len(pooled), "rejects": sum(pooled),
                   "fwer": sum(pooled) / len(pooled), "ci": [lop, hip]},
        "routing_paired_400": {"bucket": b150, "rotation": rot150,
                               "missing": miss150,
                               "rotation_share": rot150 / total},
        "fresh": fresh_stats,
        "resolution_cap_at_r": cap,
        "task33_r50": {"fwer": 0.0575, "ci": [0.0386, 0.0848], "campaigns": 400},
    }, indent=1) + "\n")
    print(f"\nwrote {HERE / 'fwer-r150.json'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True,
                    choices=("verify", "extend", "fresh", "report"))
    a = ap.parse_args()
    {"verify": stage_verify, "extend": stage_extend,
     "fresh": stage_fresh, "report": stage_report}[a.stage]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
