#!/usr/bin/env python3
"""Task 70, post hoc: can the L2 gate see a campaign whose null is tight?

    .venv/bin/python docs/reviews/task70/heterogeneous_nulls.py

**Decided after seeing the pre-registered result, and labelled as such.** The
pre-registered simulation gives every campaign a null of the same shape, so it
cannot speak to this at all. What prompted the question is the six real validation
nulls, read off `runs/campaigns/*/validation-null.json`:

    campaign             null mean    null sd    observed    sds above own null
    nq-flowcont-01         -0.7724     0.0108     -0.7867          -1.3
    nq-flowcont-5m         -0.1740     0.1267     -0.2179          -0.3
    nq-ml-02               -0.0143     0.2214     -0.1845          -0.8
    nq-ml-03               -0.0703     0.1454     -0.0557          +0.1
    nq-orderflow-01        -0.8934     0.0416     -0.1516         +17.8
    nq-orderflow-02        -0.4075     0.0733     -0.4655          -0.8

The scales differ by 20x and the locations by 0.9 Sortino, because the sign-flip
null inherits each candidate's own cost drag and turnover. `max_of_m` takes the
maximum over **raw** validation Sortino, so the campaign with the widest null
dominates the simulated maximum, and `nq-orderflow-01` -- eighteen standard
deviations above its own null -- lost the "best of M" contest to `nq-ml-03` at
plus one tenth of one.

## The question

Is the raw maximum **invalid**, or **valid but blind**? Those call for different
responses, and guessing between them is the mistake this repo's conventions exist
to prevent.

Two estimators over the same simulated families:

- **raw** — what `max_of_m` does today: maximum over observed Sortino, p from the
  simulated maximum over the campaigns' raw null draws.
- **standardised** — maximum over `(observed - null mean) / null sd`, using each
  campaign's own null *sample* moments, which is what code would have. This is
  the pivotal form the Westfall-Young step-down already in `correction.py`
  assumes.

Rule (a) is not applied. It is a profitability floor, every real null here sits
below zero, and applying it would answer a different question. **This measures
rule (b)'s calibration and power and nothing else.**

## Resolution, stated before running

4,000 families per cell. The binomial standard error at a rate of 0.05 is 0.0034
and at 0.50 it is 0.0079, so this resolves 0.065 from 0.05, and any power
difference above about 2 points.
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.core.scoring.correction import max_of_n  # noqa: E402

# The six real validation nulls, read from runs/campaigns/*/validation-null.json
# and pasted here so this file is reproducible without them.
REAL = (
    ("nq-flowcont-01", -0.7724, 0.0108),
    ("nq-flowcont-5m", -0.1740, 0.1267),
    ("nq-ml-02", -0.0143, 0.2214),
    ("nq-ml-03", -0.0703, 0.1454),
    ("nq-orderflow-01", -0.8934, 0.0416),
    ("nq-orderflow-02", -0.4075, 0.0733),
)
TIGHTEST = 0   # nq-flowcont-01, sd 0.0108
WIDEST = 2     # nq-ml-02,       sd 0.2214

FAMILIES = 4_000
NULL_DRAWS = 200
B_RESAMPLES = 2_000
Q = 0.05
DELTAS = (0.0, 2.0, 4.0, 8.0)   # planted effect, in the campaign's OWN null sds


def _wilson(count: int, n: int) -> list[float]:
    z = 1.959964
    ph = count / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def one_family(rng, *, planted: int | None, delta: float):
    """M campaigns, each exchangeable with its own null except one planted.

    `planted` names the campaign index carrying a real effect of `delta` of its
    own null standard deviations; `None` is the strict null, where every
    rejection is a false positive.
    """
    raw_nulls, std_nulls, raw_obs, std_obs = [], [], [], []
    for j, (_, m, s) in enumerate(REAL):
        null = m + s * rng.standard_normal(NULL_DRAWS)
        shift = delta * s if (planted is not None and j == planted) else 0.0
        obs = m + s * float(rng.standard_normal()) + shift
        mu, sd = float(null.mean()), float(null.std(ddof=1))
        raw_nulls.append(null.tolist())
        raw_obs.append(obs)
        std_nulls.append(((null - mu) / sd).tolist())
        std_obs.append((obs - mu) / sd)
    return raw_nulls, raw_obs, std_nulls, std_obs


def cell(*, planted: int | None, delta: float, families: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    raw_rej = std_rej = 0
    raw_winner = np.zeros(len(REAL), dtype=int)
    std_winner = np.zeros(len(REAL), dtype=int)
    for i in range(families):
        rn, ro, sn, so = one_family(rng, planted=planted, delta=delta)
        raw_winner[int(np.argmax(ro))] += 1
        std_winner[int(np.argmax(so))] += 1
        raw_rej += bool(max_of_n(rn, max(ro), b=B_RESAMPLES,
                                seed=800_000 + i, q=Q)["reject"])
        std_rej += bool(max_of_n(sn, max(so), b=B_RESAMPLES,
                                seed=800_000 + i, q=Q)["reject"])
    return {
        "planted": None if planted is None else REAL[planted][0],
        "delta_own_sds": delta,
        "families": families,
        "raw_rate": raw_rej / families, "raw_ci": _wilson(raw_rej, families),
        "std_rate": std_rej / families, "std_ci": _wilson(std_rej, families),
        "raw_winner_share": {REAL[j][0]: round(float(raw_winner[j]) / families, 4)
                             for j in range(len(REAL))},
        "std_winner_share": {REAL[j][0]: round(float(std_winner[j]) / families, 4)
                             for j in range(len(REAL))},
    }


def main() -> int:
    print(f"task 70 post hoc: heterogeneous validation nulls, M={len(REAL)}, "
          f"{FAMILIES} families/cell, R={NULL_DRAWS}, b={B_RESAMPLES}, q={Q}\n")
    print(f"{'planted in':22} {'delta':>6} {'raw':>7} {'raw 95% CI':>18} "
          f"{'standardised':>13} {'std 95% CI':>18}")
    cells = []
    for planted, label in ((None, "nothing (strict null)"),
                           (TIGHTEST, REAL[TIGHTEST][0]),
                           (WIDEST, REAL[WIDEST][0])):
        for delta in (DELTAS if planted is not None else (0.0,)):
            c = cell(planted=planted, delta=delta, families=FAMILIES,
                     seed=70_500 + int(delta * 10) + (planted or 0) * 7)
            cells.append(c)
            print(f"{label:22} {delta:6.1f} {c['raw_rate']:7.4f} "
                  f"[{c['raw_ci'][0]:.4f}, {c['raw_ci'][1]:.4f}]  "
                  f"{c['std_rate']:13.4f} "
                  f"[{c['std_ci'][0]:.4f}, {c['std_ci'][1]:.4f}]", flush=True)
        print()

    strict = cells[0]
    print("which campaign is 'the best of M' under the strict null:")
    print("  raw:          " + ", ".join(
        f"{k.replace('nq-', '')} {v:.3f}" for k, v in
        strict["raw_winner_share"].items()))
    print("  standardised: " + ", ".join(
        f"{k.replace('nq-', '')} {v:.3f}" for k, v in
        strict["std_winner_share"].items()))

    out = pathlib.Path(__file__).with_name("heterogeneous-nulls.json")
    out.write_text(json.dumps({
        "task": 70,
        "status": "POST HOC -- decided after seeing the pre-registered result",
        "question": ("is max_of_m's maximum over raw validation Sortino invalid, "
                     "or valid but blind to a campaign with a tight null?"),
        "real_validation_nulls": [
            {"campaign": c, "null_mean": m, "null_sd": s} for c, m, s in REAL],
        "constants": {"m_campaigns": len(REAL), "families": FAMILIES,
                      "null_draws": NULL_DRAWS, "b_resamples": B_RESAMPLES,
                      "q": Q, "deltas_own_sds": list(DELTAS)},
        "rule_a_applied": False,
        "cells": cells,
    }, indent=2, default=float) + "\n")
    print(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
