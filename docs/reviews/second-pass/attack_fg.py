"""F: is the null weaker for a strategy unlike the reference?
   G: is it delivered in every sub-period, or only on average?"""
import sys, json, numpy as np; sys.path.insert(0,".")
from src.core.scoring.calibration import null as N

doc = json.load(open("data/calibration/empirical-null-nq-trades.json"))
pool = np.asarray(doc["pool"], float); bl = doc["block_length"]
NS, FARS = (73, 86), (0.05, 0.01, 0.001)
rng = np.random.default_rng(11)

def realised(draws, n, far):
    sc = N._sortino(draws); lo = N._downside_count(draws)
    ok = (lo >= 5) & np.isfinite(sc)
    t = N.threshold_for(far, n, dataset="nq-trades:NQ")
    return float((sc[ok] >= t).mean()) / far, int((~ok).sum())

print("F. selective strategies: a share of sessions flat, the rest as the reference")
print(f"   {'exposure':>9} {'n':>4} " + " ".join(f"{f:>9}" for f in FARS) + "   fallback")
for share in (1.0, 0.5, 0.25, 0.10):
    for n in NS:
        d = pool[N._draw_indices(rng, pool.size, n, 300_000, bl)].copy()
        if share < 1.0:
            mask = rng.random(d.shape) > share
            d[mask] = 0.0
        row = [realised(d, n, f) for f in FARS]
        print(f"   {share:>9.2f} {n:>4} " + " ".join(f"{r[0]:>9.2f}" for r in row)
              + f"   {row[-1][1]/300_000:>6.1%}")

print("\nG. the same table, measured on each half of the training period")
half = pool.size // 2
for label, part in (("first half", pool[:half]), ("second half", pool[half:])):
    for n in NS:
        d = part[N._draw_indices(rng, part.size, n, 300_000, bl)]
        row = [realised(d, n, f)[0] for f in FARS]
        print(f"   {label:>12} n={n:<4} " + " ".join(f"{r:>9.2f}" for r in row))
print("\n   (1.00 = the promised rate; above 1.00 is anti-conservative)")
