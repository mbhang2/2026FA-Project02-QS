#!/usr/bin/env python3
"""Why R = 50 -> 150 moved the hybrid's FWER by nothing.

    python docs/reviews/task38/dilution.py

Task 34 measured `max_of_n`'s resampling bias against R on **known normal
nulls, every null at resolution R**: +0.011 at R = 50, +0.002 at R = 150. It
then restated Task 33's 0.058 as "about 0.047 by correction". Task 38 ran the
correction out and got **0.060**, one campaign higher, not eleven lower.

The obvious candidate explanation is dilution: in the hybrid only about a fifth
of nulls are rotation nulls at resolution R, and the rest are profile buckets
holding 200+ scores. `max_of_n`'s bias comes from a null truncating its own
upper tail under resampling, and a campaign's simulated maximum is dominated by
whichever null has the fattest upper tail -- so if the buckets dominate, moving
the rotation arm from 50 to 150 changes almost nothing.

This does not argue that. It measures it, on the runs already on disk, with no
engine involved: the same campaigns re-scored using **only** their
rotation-routed trials, at R = 50 and at R = 150. If the bias is real and
dilution is the reason it did not show, it shows here.
"""

from __future__ import annotations

import json
import pathlib
import statistics as st
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "docs" / "reviews" / "task38"))
HERE = pathlib.Path(__file__).resolve().parent
T33 = REPO / "docs" / "reviews" / "task33"
N_TRIALS, Q, CAMPAIGNS = 10, 0.05, 400


def main() -> None:
    from hybrid_fwer_r150 import profile_table, wilson
    from src.core.scoring.correction import max_of_n

    table = profile_table()
    t33 = json.loads((T33 / "campaigns.json").read_text())
    ext = json.loads((HERE / "extend.json").read_text())
    base, routes = t33["base"], t33["routes"]

    r50, r150 = {}, {}
    for slot, i in enumerate(t33["fallback_index"]):
        r50[i] = [x for x in t33["rotation"][slot * 50:(slot + 1) * 50]
                  if x is not None]
    add = ext["added_per_candidate"]
    for slot, i in enumerate(ext["fallback_index"]):
        chunk = ext["rotation"][slot * add:(slot + 1) * add]
        r150[i] = r50[i] + [x for x in chunk if x is not None]

    sizes50 = [len(v) for v in r50.values()]
    sizes150 = [len(v) for v in r150.values()]
    bsizes = [len(v) for v in table.buckets.values() if len(v) >= 200]
    print("NULL SIZES -- what each arm of the hybrid actually resolves")
    print(f"  rotation nulls, R = 50   n = {len(sizes50)}  "
          f"median {st.median(sizes50):.0f}  min {min(sizes50)}")
    print(f"  rotation nulls, R = 150  n = {len(sizes150)}  "
          f"median {st.median(sizes150):.0f}  min {min(sizes150)}")
    print(f"  profile buckets in use   n = {len(bsizes)}  "
          f"sizes {sorted(bsizes)}")
    print()

    # how much of a campaign is rotation
    per_campaign = []
    for c in range(CAMPAIGNS):
        rot = sum(1 for j in range(c * N_TRIALS, (c + 1) * N_TRIALS)
                  if routes[j] and routes[j]["route"] == "rotation")
        per_campaign.append(rot)
    print("ROTATION TRIALS PER CAMPAIGN (of 10)")
    print(f"  mean {st.fmean(per_campaign):.2f}   median "
          f"{st.median(per_campaign):.0f}   "
          f"campaigns with none {sum(1 for x in per_campaign if x == 0)}   "
          f"with >= 5 {sum(1 for x in per_campaign if x >= 5)}")
    print()

    # the same campaigns, rotation-routed trials only
    def fwer(nulls, rotation_only: bool):
        rejects = 0
        used = 0
        for c in range(CAMPAIGNS):
            samples, observed = [], []
            for j in range(c * N_TRIALS, (c + 1) * N_TRIALS):
                route = routes[j]
                if route is None or base[j]["score"] is None:
                    continue
                if route["route"] == "bucket":
                    if rotation_only:
                        continue
                    samples.append(table.buckets[route["bucket"]])
                else:
                    s = nulls.get(j, [])
                    if len(s) < 25:
                        continue
                    samples.append(s)
                observed.append(base[j]["score"])
            if not samples:
                continue
            used += 1
            rejects += bool(max_of_n(samples, max(observed), b=10_000,
                                     q=Q)["reject"])
        return rejects, used

    out = {}
    for label, nulls, rot_only in (("full hybrid, R=50", r50, False),
                                   ("full hybrid, R=150", r150, False),
                                   ("rotation-routed trials only, R=50", r50, True),
                                   ("rotation-routed trials only, R=150", r150, True)):
        k, n = fwer(nulls, rot_only)
        lo, hi = wilson(k, n)
        out[label] = {"rejects": k, "campaigns": n, "fwer": k / n,
                      "ci": [lo, hi]}
        print(f"  {label:38s} {k:>3}/{n:<4} = {k / n:.4f}  [{lo:.3f}, {hi:.3f}]")

    # Counting rejections at a threshold throws away almost everything: 23 vs
    # 24 of 400 is one campaign. The p-value itself is the continuous quantity
    # the bias acts on, it is paired campaign by campaign, and `max_of_n` runs
    # at a fixed seed so the B = 10,000 resampling noise is common to both arms.
    print()
    print("PAIRED p-VALUES -- the same campaigns, both resolutions")
    for label, rot_only in (("full hybrid", False),
                            ("rotation-routed trials only", True)):
        pairs = []
        for c in range(CAMPAIGNS):
            got = {}
            for tag, nulls in (("p50", r50), ("p150", r150)):
                samples, observed = [], []
                for j in range(c * N_TRIALS, (c + 1) * N_TRIALS):
                    route = routes[j]
                    if route is None or base[j]["score"] is None:
                        continue
                    if route["route"] == "bucket":
                        if rot_only:
                            continue
                        samples.append(table.buckets[route["bucket"]])
                    else:
                        s = nulls.get(j, [])
                        if len(s) < 25:
                            continue
                        samples.append(s)
                    observed.append(base[j]["score"])
                got[tag] = (max_of_n(samples, max(observed), b=10_000, q=Q)["p"]
                            if samples else None)
            if got["p50"] is not None and got["p150"] is not None:
                pairs.append((got["p50"], got["p150"]))
        d = [b - a for a, b in pairs]
        up = sum(1 for x in d if x > 0)
        down = sum(1 for x in d if x < 0)
        same = sum(1 for x in d if x == 0)
        print(f"  {label:30s} n={len(pairs)}  mean delta {st.fmean(d):+.5f}  "
              f"median {st.median(d):+.5f}")
        print(f"  {'':30s} p rose in {up}, fell in {down}, unchanged in {same}")
        out[f"paired_p::{label}"] = {
            "n": len(pairs), "mean_delta": st.fmean(d),
            "median_delta": st.median(d), "rose": up, "fell": down,
            "unchanged": same}

    # The mechanism itself. `bias(R)` describes a cap: resampling with
    # replacement cannot produce a simulated maximum above the largest draw the
    # null happens to contain, so a small null truncates its own upper tail and
    # the test over-rejects. That only bites when the observed statistic is at
    # or beyond that largest draw. Is it?
    print()
    print("DOES THE CAP EVEN BIND? -- observed best vs the largest null draw")
    cap = {}
    for tag, nulls in (("R=50", r50), ("R=150", r150)):
        binds, tot, margins = 0, 0, []
        for c in range(CAMPAIGNS):
            best = nullmax = None
            for j in range(c * N_TRIALS, (c + 1) * N_TRIALS):
                route = routes[j]
                if route is None or base[j]["score"] is None:
                    continue
                s = (table.buckets[route["bucket"]] if route["route"] == "bucket"
                     else nulls.get(j, []))
                if not s:
                    continue
                nullmax = max(s) if nullmax is None else max(nullmax, max(s))
                b = base[j]["score"]
                best = b if best is None else max(best, b)
            if best is None or nullmax is None:
                continue
            tot += 1
            margins.append(best - nullmax)
            binds += best >= nullmax
        cap[tag] = {"binds": binds, "campaigns": tot, "share": binds / tot,
                    "median_margin": st.median(margins)}
        print(f"  {tag:6s} observed best reaches the largest null draw in "
              f"{binds}/{tot} = {binds / tot:.1%} of campaigns")
        print(f"  {'':6s} median (best - largest null draw) = "
              f"{st.median(margins):+.4f}")
    out["cap_binding"] = cap

    a = out["rotation-routed trials only, R=50"]["fwer"]
    b = out["rotation-routed trials only, R=150"]["fwer"]
    c = out["full hybrid, R=50"]["fwer"]
    d = out["full hybrid, R=150"]["fwer"]
    print()
    print("THE CONTRAST")
    print(f"  rotation arm alone, R=50 -> R=150:  {a:+.4f} -> {b:+.4f}  "
          f"({b - a:+.4f})")
    print(f"  full hybrid,        R=50 -> R=150:  {c:+.4f} -> {d:+.4f}  "
          f"({d - c:+.4f})")
    print(f"  Task 34's bias(50) - bias(150) predicted a drop of -0.009")
    (HERE / "dilution.json").write_text(json.dumps({
        "null_sizes": {"rotation_r50_median": st.median(sizes50),
                       "rotation_r150_median": st.median(sizes150),
                       "buckets_in_use": sorted(bsizes)},
        "rotation_trials_per_campaign": {
            "mean": st.fmean(per_campaign), "median": st.median(per_campaign),
            "none": sum(1 for x in per_campaign if x == 0),
            "five_or_more": sum(1 for x in per_campaign if x >= 5)},
        "fwer": out,
        "predicted_drop_from_task34": -0.009,
        "cap_binding": out.get("cap_binding"),
    }, indent=1) + "\n")
    print(f"\nwrote {HERE / 'dilution.json'}")


if __name__ == "__main__":
    main()
