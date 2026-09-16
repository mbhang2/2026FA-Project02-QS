#!/usr/bin/env python3
"""Build the empirical null and commit it.

    python scripts/tabulate_null.py                     # synthetic
    python scripts/tabulate_null.py --dataset nq-trades # real NQ training sessions
    python scripts/tabulate_null.py --runs 200 --trials 100000

Runs the immutable reference strategy, which has no edge by construction, pools
its per-session returns, and bootstraps the sampling distribution of the score at
each session count. The output is committed so thresholds are auditable rather
than regenerated, and so a run does not re-simulate a null to score one trial.

**One null per dataset, and the table says which.** A threshold measured on one
return-generating process says nothing about another: the synthetic sessions have
far less dispersion than real NQ ones, so the synthetic table sets thresholds far
too low for real data. `threshold_for`/`far_for` refuse a table whose tag does not
match the dataset asked about.

**The real-data null uses a block bootstrap**, with the block length measured
from the pool's autocorrelation rather than chosen. An i.i.d. resample destroys
volatility clustering and understates the variance of an N-session statistic.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.scoring.calibration import null as nullmod  # noqa: E402


def verify(pool, block_length: int, table_path: Path) -> None:
    """Promised versus measured, on sessions the table was not built from.

    Also run at block length 1, because the comparison is the evidence: a table
    is not verified by being self-consistent, it is verified by delivering its
    promise on data it has not seen, and the alternative has to be measured the
    same way to be a comparison at all.
    """
    out = table_path.with_name(table_path.stem + "-verification.json")
    doc = {
        "table": table_path.name,
        "question": (
            "Is the FAR column a p-value column? Benjamini-Hochberg needs "
            "validity -- P(p <= a) <= a for every a -- not exact uniformity. "
            "Conservative costs power and stays valid; anti-conservative makes "
            "the reported false-discovery rate precise and wrong."
        ),
        "runs": {},
    }
    for bl in sorted({block_length, 1}, reverse=True):
        result = nullmod.verify_held_out(pool, block_length=bl)
        doc["runs"][str(bl)] = result
        label = "measured block" if bl == block_length else "i.i.d. (block 1)"
        print()
        print(f"held out, {label}: build {result['build_sessions']} sessions, "
              f"check {result['check_sessions']}  worst ratio "
              f"{result['worst_ratio']:.2f}  anti-conservative cells "
              f"{len(result['anti_conservative_cells'])}")
        print(f"    {'n':>4} {'far':>7} {'threshold':>10} {'measured':>10} {'meas/far':>9}")
        for row in result["rows"]:
            m = "n/a" if row["measured"] is None else f"{row['measured']:.4f}"
            r = "" if row.get("ratio") is None else f"{row['ratio']:.2f}"
            print(f"    {row['n']:>4} {row['far']:>7} {row['threshold']:>10.4f} "
                  f"{m:>10} {r:>9}")
    for c in doc["runs"][str(block_length)]["far_column"]:
        print(f"    FAR column n={c['n']}: valid inside the grid: "
              f"{c['valid_inside_grid']}  |  KS on the interior "
              f"{c['ks_statistic_interior']:.4f} (1% critical "
              f"{c['ks_critical_0.01']:.4f})")
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {out.relative_to(REPO_ROOT)}")


def arithmetic_provenance() -> dict:
    """Which machine and which arithmetic produced this table.

    Task 28 made scores bit-identical across platforms by summing money with
    `math.fsum` rather than `sum`: CPython 3.12 compensates float summation and
    3.11 does not, so the same inputs gave different totals on the two
    interpreters. A table is now reproducible anywhere -- and a table that does
    not record where it came from cannot be checked against that claim, so it
    records it.
    """
    import platform

    return {
        "platform": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "numpy": __import__("numpy").__version__,
        "money_summation": "math.fsum (exactly rounded; see src/core/types.py)",
        "session_returns_summation": "math.fsum",
    }


def tabulate_real(args) -> int:
    """The null of a real dataset: real sessions, real clustering, block bootstrap."""
    import os

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import inventory as inv

    from src.core.data.nq import NQDataset, NQSpec

    t0 = time.monotonic()
    out = args.out
    if out == nullmod.NULL_PATH:
        out = nullmod.null_path_for(args.dataset)

    ds = NQDataset(inv.s3_client(12), os.environ["R2_BUCKET"], NQSpec())
    print(f"dataset   {ds.name}  digest {ds.digest()}", flush=True)
    print("running the reference strategy over the TRAINING split ...", flush=True)
    pool, provenance = nullmod.real_session_returns(ds)
    print(f"  {provenance['sessions_in_pool']:,} sessions pooled "
          f"({provenance['sessions_excluded']} excluded) "
          f"in {time.monotonic() - t0:.0f}s", flush=True)
    if provenance["contaminated_sessions_remaining_in_pool"]:
        print("  WARNING: sessions still carrying a position remain in the pool: "
              f"{provenance['contaminated_sessions_remaining_in_pool']}", flush=True)

    measured = nullmod.measure_block_length(pool)
    block = args.block_length or measured["block_length"]
    print(f"  acf(1) returns {measured['lag1_returns']:+.4f}  "
          f"|returns| {measured['lag1_abs_returns']:+.4f}  "
          f"band +/-{measured['significance_band']:.4f}")
    print(f"  block length {block} "
          f"({'measured' if not args.block_length else 'overridden'})", flush=True)

    doc = nullmod.tabulate(pool, trials=args.trials, block_length=block,
                           dataset=nullmod.null_key(ds.name))
    doc["provenance"] = {
        **provenance,
        "block_length_measurement": measured,
        "block_length_source": "measured" if not args.block_length else "overridden",
        "arithmetic": arithmetic_provenance(),
        "wall_clock_s": round(time.monotonic() - t0, 1),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out.relative_to(REPO_ROOT)}  in {time.monotonic() - t0:.0f}s")

    if args.verify:
        verify(pool, block, out)

    print()
    # Read back through threshold_for so session counts off the grid -- 86, the
    # validation split -- are interpolated the way a caller would get them.
    for n in (30, 50, 86, 100):
        lo = nullmod.threshold_for(0.05, n, dataset=ds.name, path=out)
        hi = nullmod.threshold_for(0.001, n, dataset=ds.name, path=out)
        print(f"  sortino N={n:<4} far=0.05 -> {lo:+.4f}   far=0.001 -> {hi:+.4f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=120,
                    help="independent no-signal runs feeding the session-return pool")
    ap.add_argument("--sessions", type=int, default=175)
    ap.add_argument("--ticks-per-session", type=int, default=390)
    ap.add_argument("--trials", type=int, default=500_000,
                    help="bootstrap draws per session count. At 50,000 the "
                         "far=0.001 quantile rests on ~50 exceedances and its "
                         "sampling sd is 0.026; at 500,000 it is 0.008.")
    ap.add_argument("--seed0", type=int, default=10_000)
    ap.add_argument("--out", type=Path, default=nullmod.NULL_PATH)
    ap.add_argument("--pool-from", type=Path,
                    help="reuse the committed session-return pool instead of "
                         "re-simulating it; the bootstrap is then instant")
    ap.add_argument("--dataset", default="synthetic",
                    help="which return-generating process to measure: "
                         "'synthetic', or 'nq-trades' for real NQ training sessions")
    ap.add_argument("--verify", action="store_true",
                    help="also tabulate on one contiguous half of the pool and "
                         "measure realized false acceptance on the other")
    ap.add_argument("--block-length", type=int,
                    help="mean block for the stationary bootstrap; measured from "
                         "the pool when omitted. 1 reproduces the i.i.d. resample.")
    args = ap.parse_args()

    if args.dataset != "synthetic":
        return tabulate_real(args)

    t0 = time.monotonic()
    if args.pool_from:
        pool = json.loads(args.pool_from.read_text())["pool"]
        print(f"reusing {len(pool):,} pooled null session returns from "
              f"{args.pool_from}", flush=True)
    else:
        print(f"simulating {args.runs} no-signal runs ...", flush=True)
        pool = nullmod.null_session_returns(
            runs=args.runs,
            n_sessions=args.sessions,
            ticks_per_session=args.ticks_per_session,
            seed0=args.seed0,
        )
        print(f"  pooled {len(pool):,} null session returns "
              f"in {time.monotonic() - t0:.0f}s", flush=True)

    # Measured, not assumed -- and it used to be assumed. The rationale for
    # block length 1 here was that the generator seeds every session
    # independently, which stopped being true the moment Task 17 gave it
    # volatility clustering. A null that is exempt from its own method because
    # of a comment is the shape of every defect this project keeps finding.
    measured = nullmod.measure_block_length(pool)
    block = args.block_length or measured["block_length"]
    print(f"  acf(1) returns {measured['lag1_returns']:+.4f}  "
          f"|returns| {measured['lag1_abs_returns']:+.4f}  "
          f"band +/-{measured['significance_band']:.4f}")
    print(f"  block length {block} "
          f"({'measured' if not args.block_length else 'overridden'})", flush=True)
    doc = nullmod.tabulate(pool, trials=args.trials, block_length=block,
                           dataset="synthetic")
    if args.verify:
        verify(pool, block, args.out)

    doc["provenance"] = {
        "runs": args.runs,
        "sessions_per_run": args.sessions,
        "ticks_per_session": args.ticks_per_session,
        "seed0": args.seed0,
        "strategy": "src/core/scoring/calibration/reference.py::ReferenceStrategy",
        "fill_model": "unfavorable",
        "block_length_measurement": measured,
        "block_length_source": "measured" if not args.block_length else "overridden",
        "arithmetic": arithmetic_provenance(),
        "wall_clock_s": round(time.monotonic() - t0, 1),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out.relative_to(REPO_ROOT)}")

    print()
    for metric in doc["metrics"]:
        for n in (30, 50, 100):
            cell = doc["table"][metric].get(str(n))
            if cell and cell["trials"]:
                q = cell["quantiles"]
                print(f"{metric:<22} N={n:<4} share={cell['share_of_draws']:.3f} "
                      f"far=0.05 -> {q.get('0.05')}  far=0.001 -> {q.get('0.001')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
