import sys, json, numpy as np; sys.path.insert(0,".")
from src.core.scoring.calibration import null as N
doc = json.load(open("data/calibration/empirical-null-nq-trades.json"))
pool = np.asarray(doc["pool"], float); bl = doc["block_length"]; half = pool.size//2

print("H1. Task 19's held-out check, both directions")
for label, (build, check) in (("build 1st / check 2nd", (pool[:half], pool[half:])),
                              ("build 2nd / check 1st", (pool[half:], pool[:half]))):
    t = N.tabulate(build, trials=300_000, n_grid=(73, 86), far_grid=(0.05,0.01,0.001),
                   seed=5, block_length=bl, dataset="x")
    rng = np.random.default_rng(6)
    out = []
    for n in (73, 86):
        d = check[N._draw_indices(rng, check.size, n, 300_000, bl)]
        sc = N._sortino(d); lo = N._downside_count(d)
        e = sc[(lo>=5) & np.isfinite(sc)]
        for f in (0.05, 0.01, 0.001):
            q = t["table"]["sortino_per_session"][str(n)]["quantiles"][str(f)]
            out.append(float((e>=q).mean())/f)
    print(f"   {label}: worst ratio {max(out):.2f}   {[round(x,2) for x in out]}")

print("\nH2. is the Sharpe fallback an easier gate than Sortino?")
for n in (73, 86):
    a = N.threshold_for(0.001, n, dataset="nq-trades:NQ", metric="sortino_per_session")
    b = N.threshold_for(0.001, n, dataset="nq-trades:NQ", metric="sharpe_per_session")
    print(f"   n={n}: sortino {a:+.4f}   sharpe {b:+.4f}")
print("   a strategy with <5 losing sessions is scored on Sharpe, against Sharpe's null")
