#!/usr/bin/env python3
"""Part B: the transfer factor, measured as a distribution with a control.

    python docs/reviews/task28/transfer.py --k 6

Task 27 reported 3.12 as the worst cell of a *single* 2-fold split -- one
realization of a diagnostic with a free parameter (which half builds the table),
where only one setting had ever been run. Inflating the FAR column by 3.12 would
be as arbitrary as tuning the block length to the same diagnostic.

So: block the 384-session training pool into K contiguous windows, tabulate on
one and measure realized false acceptance on another, for every ordered pair, and
report the distribution per (n, far) cell.

**The control is what makes the number mean anything.** A 64-session build window
estimates its own tail far worse than a 384-session one, so a raw cross-window
ratio conflates regime transfer with small-build-sample error. Same-window pairs
-- build and test on disjoint halves of one window -- are the no-transfer
baseline at the same build size. The transfer factor is the cross-window ratio
*relative to* that baseline; the raw ratio is not.

Everything here is inside the 384-session training split. Validation and holdout
are untouched.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from src.core.scoring.calibration import null as nullmod  # noqa: E402

TABLE = REPO / "data" / "calibration" / "empirical-null-nq-trades.json"
N_GRID = (30, 50, 73, 86, 100)


def measure(build, check, *, block_length, trials, far_grid, seed):
    """Realized FAR on `check` against thresholds tabulated on `build`.

    The same computation `verify_held_out` does, factored out so it can be run
    over arbitrary window pairs rather than only the two halves.
    """
    doc = nullmod.tabulate(build, trials=trials, n_grid=N_GRID, far_grid=far_grid,
                           seed=seed, block_length=block_length,
                           dataset="transfer-build")
    rng = np.random.default_rng(seed + 1)
    out = {}
    for n in N_GRID:
        draws = check[nullmod._draw_indices(rng, check.size, n, trials, block_length)]
        scores = nullmod._sortino(draws)
        losers = nullmod._downside_count(draws)
        eligible = scores[(losers >= 5) & np.isfinite(scores)]
        for far in far_grid:
            promised = doc["table"]["sortino_per_session"][str(n)]["quantiles"][str(far)]
            if promised is None or eligible.size == 0:
                out[(n, far)] = None
                continue
            out[(n, far)] = float((eligible >= promised).mean() / far)
    return out


def quantile(values, q: float) -> float:
    return float(np.quantile(np.asarray(values, dtype=float), q))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=6,
                    help="contiguous windows the 384-session pool is cut into")
    ap.add_argument("--trials", type=int, default=100_000)
    args = ap.parse_args()

    doc = json.loads(TABLE.read_text())
    pool = np.asarray(doc["pool"], dtype=float)
    block = doc["block_length"]
    far_grid = tuple(doc["far_grid"])
    size = len(pool) // args.k
    windows = [pool[i * size:(i + 1) * size] for i in range(args.k)]

    print(f"pool {len(pool)} training sessions, K={args.k} contiguous windows of "
          f"{size}, block {block}, {args.trials:,} trials per cell")
    print(f"cross-window ordered pairs: {args.k * (args.k - 1)}   "
          f"same-window control pairs: {args.k * 2}")
    print()

    t0 = time.monotonic()
    cross: dict[tuple, list[float]] = {}
    same: dict[tuple, list[float]] = {}

    # the control first: build and test on disjoint halves of ONE window, both
    # directions, at the same build size as the cross-window pairs
    for w, win in enumerate(windows):
        half = len(win) // 2
        for build, check in ((win[:half], win[half:]), (win[half:], win[:half])):
            got = measure(build, check, block_length=block, trials=args.trials,
                          far_grid=far_grid, seed=4242 + w)
            for key, ratio in got.items():
                if ratio is not None:
                    same.setdefault(key, []).append(ratio)
    print(f"  control done in {time.monotonic() - t0:.0f}s", flush=True)

    for i, build in enumerate(windows):
        for j, check in enumerate(windows):
            if i == j:
                continue
            got = measure(build, check, block_length=block, trials=args.trials,
                          far_grid=far_grid, seed=4242 + 10 * i + j)
            for key, ratio in got.items():
                if ratio is not None:
                    cross.setdefault(key, []).append(ratio)
        print(f"  build window {i} done, {time.monotonic() - t0:.0f}s", flush=True)

    ns = sorted({n for n, _ in cross})
    fars = sorted({f for _, f in cross})

    def table(source, stat) -> str:
        head = f"{'n':>5} " + "".join(f"{f:>9}" for f in fars)
        lines = [head, "-" * len(head)]
        for n in ns:
            cells = []
            for f in fars:
                vals = source.get((n, f))
                cells.append("        -" if not vals else f"{stat(vals):>9.2f}")
            lines.append(f"{n:>5} " + "".join(cells))
        return "\n".join(lines)

    for label, source in (("CROSS-WINDOW (raw)", cross), ("SAME-WINDOW (control)", same)):
        for name, stat in (("median", lambda v: quantile(v, 0.5)),
                           ("p90", lambda v: quantile(v, 0.9)),
                           ("max", max)):
            print(f"=== {label} -- {name} ===")
            print(table(source, stat))
            print()

    print("=== TRANSFER FACTOR: cross-window / same-window, per cell ===")
    print("    (median over cross pairs divided by median over control pairs)")
    rel = {k: quantile(v, 0.5) / quantile(same[k], 0.5)
           for k, v in cross.items() if same.get(k)}
    print(table({k: [v] for k, v in rel.items()}, lambda v: v[0]))
    print()
    rel90 = {k: quantile(v, 0.9) / quantile(same[k], 0.5)
             for k, v in cross.items() if same.get(k)}
    print("=== TRANSFER FACTOR at the p90 of the cross distribution ===")
    print(table({k: [v] for k, v in rel90.items()}, lambda v: v[0]))
    print()

    allsame = [x for v in same.values() for x in v]
    print(f"same-window control, pooled over all cells: median "
          f"{quantile(allsame, 0.5):.2f}  p90 {quantile(allsame, 0.9):.2f}  "
          f"max {max(allsame):.2f}")
    print(f"transfer factor, pooled: median {quantile(list(rel.values()), 0.5):.2f}  "
          f"p90 {quantile(list(rel.values()), 0.9):.2f}  "
          f"max {max(rel.values()):.2f}")
    print(f"at n=86: " + "  ".join(
        f"far={f} -> {rel[(86, f)]:.2f}" for f in (0.0005, 0.001, 0.01, 0.05)
        if (86, f) in rel))
    print(f"at n=73: " + "  ".join(
        f"far={f} -> {rel[(73, f)]:.2f}" for f in (0.0005, 0.001, 0.01, 0.05)
        if (73, f) in rel))

    out = {
        "k": args.k, "window_sessions": size, "block_length": block,
        "trials": args.trials,
        "cross": {f"{n}|{f}": v for (n, f), v in cross.items()},
        "same": {f"{n}|{f}": v for (n, f), v in same.items()},
        "transfer_median": {f"{n}|{f}": v for (n, f), v in rel.items()},
        "transfer_p90": {f"{n}|{f}": v for (n, f), v in rel90.items()},
    }
    (pathlib.Path(__file__).parent / f"transfer-k{args.k}.json").write_text(
        json.dumps(out, indent=1, sort_keys=True))
    print(f"\nwall {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
