#!/usr/bin/env python3
"""Task 70: does an adaptive chooser break the L2 gate's error control?

    .venv/bin/python docs/reviews/task70/adaptive_selection.py \
        --out docs/reviews/task70/results.json

Pre-registered in this directory's README before it ran. Read section 4 there for
the arms and section 6 for the one place the model simplifies; this file is the
arithmetic, not the argument.

Offline. No market data, no campaign, no trial count spent -- nothing here looks
at our data, so by section 4 of the pre-registration none of it is a look.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.core.scoring.correction import max_of_m  # noqa: E402

# --- section 5 of the README. Changing one of these invalidates the table. ----
M_CAMPAIGNS = 6
N_TRIALS = 20
FAMILIES = 12
EXPLOIT_P = 0.8
SESSIONS = 2_000
RHO_GRID = (0.0, 0.1, 0.2, 0.3, 0.5, 0.8)
NULL_DRAWS = 200          # above the R >= 150 operating rule
B_RESAMPLES = 2_000
Q = 0.05
SEED_BASE = 700_000

ARMS = ("fixed", "adaptive_train", "adaptive_validation")


def _rel(path: pathlib.Path) -> str:
    """A readable path that never raises -- `relative_to` throws for anything
    outside the tree, which is every scratch directory a smoke run writes to."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _cell_seed(arm: str, rho: float) -> int:
    """A deterministic per-cell seed that is not `hash()`.

    `hash()` of a str is salted per process, which already cost this project a
    campaign whose null seed differed in three interpreters (`l2_gate.py`).
    """
    import hashlib
    key = f"task70/{arm}/{rho:.4f}".encode()
    return 70_000 + int(hashlib.sha256(key).hexdigest()[:8], 16) % 900_000


def one_session(arm: str, rho: float, rng: np.random.Generator) -> list[dict]:
    """M campaigns under one arm. Returns `max_of_m`-shaped winner entries.

    The pool is what carrying a champion means: in `fixed` each campaign's winner
    is the best of its own N draws, and in the adaptive arms a campaign that
    continues a family takes the best of everything that family has accumulated.
    That is the inflation being measured -- the pool grows while the L2
    correction stays at M.
    """
    pools: dict[int, list[float]] = {}
    best_train: dict[int, float] = {}
    best_valid: dict[int, float] = {}
    fresh = 0
    winners: list[dict] = []

    for k in range(M_CAMPAIGNS):
        # ---- which family does this campaign run under? ---------------------
        if arm == "fixed" or k == 0 or not best_train:
            family = fresh
            fresh += 1
        else:
            explore = rng.random() > EXPLOIT_P or fresh >= FAMILIES
            if explore and fresh < FAMILIES:
                family = fresh
                fresh += 1
            else:
                seen = best_valid if arm == "adaptive_validation" else best_train
                family = max(seen, key=seen.__getitem__)

        # ---- N train draws, added to that family's pool ---------------------
        draws = rng.standard_normal(N_TRIALS)
        if arm == "fixed":
            pool = list(draws)          # no champion carried across campaigns
        else:
            pool = pools.setdefault(family, [])
            pool.extend(draws.tolist())
        t = max(pool)

        # ---- the winner's validation statistic, and its own null ------------
        v = rho * t + math.sqrt(max(0.0, 1.0 - rho * rho)) * float(
            rng.standard_normal())
        best_train[family] = max(best_train.get(family, -np.inf), t)
        best_valid[family] = max(best_valid.get(family, -np.inf), v)
        winners.append({
            "campaign_id": f"c{k}",
            "validation_primary": v,
            "validation_null": rng.standard_normal(NULL_DRAWS),
        })
    return winners


def cell(arm: str, rho: float, *, sessions: int) -> dict:
    rng = np.random.default_rng(_cell_seed(arm, rho))
    rejects = passes = rule_a = 0
    p_values: list[float] = []
    observed: list[float] = []
    for i in range(sessions):
        winners = one_session(arm, rho, rng)
        out = max_of_m(winners, b=B_RESAMPLES, seed=SEED_BASE + i, q=Q)
        rejects += bool(out["reject"])
        passes += bool(out["passes"])
        rule_a += bool(out["rule_a_pass"])
        p_values.append(float(out["p"]))
        observed.append(float(out["observed_best"]))

    def ci(count: int) -> list[float]:
        # Wilson, because a normal interval on a rate near 0.05 with 2,000 draws
        # is wrong at the edges and this table is read at the edges.
        z, n = 1.959964, sessions
        ph = count / n
        d = 1 + z * z / n
        c = (ph + z * z / (2 * n)) / d
        h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
        return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]

    return {
        "arm": arm, "rho": rho, "sessions": sessions,
        "rule_b_reject_rate": rejects / sessions,
        "rule_b_ci": ci(rejects),
        "rule_a_rate": rule_a / sessions,
        "pass_rate": passes / sessions,
        "pass_ci": ci(passes),
        "median_p": float(np.median(p_values)),
        "mean_observed_best": float(np.mean(observed)),
    }


# --------------------------------------------------------------------------- #
# section 8: what is rho actually, on the six campaigns we have?
# --------------------------------------------------------------------------- #
def empirical_rho() -> dict:
    """Pearson r over (train, validation) for the SAME winner, real campaigns.

    `runs/campaigns/<id>/validation.json` holds `train.primary` and
    `validation.primary` for one candidate -- the winner -- which is exactly the
    within-candidate pairing rho is defined over in section 3, not a
    campaign-level correlation that would confound selection with persistence.

    Six points. The interval spans nearly everything and the README says so; this
    is in the record because an assumed rho is worse than a badly-estimated one.
    """
    rows = []
    for f in sorted((ROOT / "runs" / "campaigns").glob("*/validation.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        t = (d.get("train") or {}).get("primary")
        v = (d.get("validation") or {}).get("primary")
        if t is None or v is None:
            continue
        rows.append({"campaign": f.parent.name, "winner_digest": d.get("winner_digest"),
                     "train": float(t), "validation": float(v)})

    out = {"pairs": rows, "n": len(rows),
           "definition": ("within-candidate: the same winner's primary on train "
                          "and on validation, from validation.json")}
    if len(rows) >= 3:
        a = np.array([r["train"] for r in rows])
        b = np.array([r["validation"] for r in rows])
        if a.std() > 0 and b.std() > 0:
            r = float(np.corrcoef(a, b)[0, 1])
            out["pearson_r"] = r
            # Fisher z, for the interval this sample size actually supports.
            n = len(rows)
            if n > 3 and abs(r) < 1:
                z = 0.5 * math.log((1 + r) / (1 - r))
                se = 1.0 / math.sqrt(n - 3)
                lo, hi = z - 1.959964 * se, z + 1.959964 * se
                out["pearson_ci"] = [round(math.tanh(lo), 4), round(math.tanh(hi), 4)]
            out["note"] = (
                f"n = {len(rows)}. The interval below spans nearly everything, "
                "which is the finding: rho is not known to a precision that "
                "distinguishes any two columns of the table above. The number is "
                "recorded so rho is measured rather than assumed.")
        else:
            out["note"] = "one side has zero variance; r is undefined"
    else:
        out["note"] = ("fewer than three campaigns have a validation.json with "
                       "both primaries; r is not computed")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=pathlib.Path,
                    default=pathlib.Path(__file__).with_name("results.json"))
    ap.add_argument("--sessions", type=int, default=SESSIONS)
    ap.add_argument("--rho", type=float, action="append", default=None)
    a = ap.parse_args()
    grid = tuple(a.rho) if a.rho else RHO_GRID

    print(f"task 70: {len(ARMS)} arms x {len(grid)} rho x {a.sessions} sessions, "
          f"M={M_CAMPAIGNS} N={N_TRIALS} R={NULL_DRAWS} b={B_RESAMPLES} q={Q}\n")
    print(f"{'arm':22} {'rho':>5} {'rule b':>8} {'95% CI':>18} "
          f"{'rule a':>8} {'both':>8} {'med p':>7} {'E[best]':>8}")
    cells = []
    t0 = time.monotonic()
    for rho in grid:
        for arm in ARMS:
            c = cell(arm, rho, sessions=a.sessions)
            cells.append(c)
            print(f"{arm:22} {rho:5.2f} {c['rule_b_reject_rate']:8.4f} "
                  f"[{c['rule_b_ci'][0]:.4f}, {c['rule_b_ci'][1]:.4f}]  "
                  f"{c['rule_a_rate']:8.4f} {c['pass_rate']:8.4f} "
                  f"{c['median_p']:7.4f} {c['mean_observed_best']:8.3f}",
                  flush=True)
        print()

    rho_hat = empirical_rho()
    print(f"empirical rho: n={rho_hat['n']} "
          f"r={rho_hat.get('pearson_r', 'n/a')}")
    print("  " + str(rho_hat.get("pearson_ci", "")))
    for r in rho_hat["pairs"]:
        print(f"  {r['campaign']:22} train {r['train']:+.4f}  "
              f"validation {r['validation']:+.4f}")

    payload = {
        "task": 70,
        "question": ("does a chooser that picks campaign k+1's family from "
                     "campaigns 1..k's train results inflate the L2 gate's "
                     "false-positive rate above q?"),
        "constants": {"m_campaigns": M_CAMPAIGNS, "n_trials": N_TRIALS,
                      "families": FAMILIES, "exploit_p": EXPLOIT_P,
                      "sessions": a.sessions, "null_draws": NULL_DRAWS,
                      "b_resamples": B_RESAMPLES, "q": Q,
                      "rho_grid": list(grid)},
        "resolvable": ("binomial SE at a rate of 0.05 with "
                       f"{a.sessions} sessions is "
                       f"{math.sqrt(0.05 * 0.95 / a.sessions):.4f}; this "
                       "resolves 0.07 from 0.05 and not 0.055 from 0.05"),
        "cells": cells,
        "empirical_rho": rho_hat,
        "elapsed_seconds": round(time.monotonic() - t0, 1),
    }
    a.out.write_text(json.dumps(payload, indent=2, default=float) + "\n")
    print(f"\nwrote {_rel(a.out)}  ({payload['elapsed_seconds']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
