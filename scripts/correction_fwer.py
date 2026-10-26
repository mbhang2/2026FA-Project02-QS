#!/usr/bin/env python3
"""Measure FWER for the `max_of_n` variants, under the global null.

    python scripts/correction_fwer.py --campaigns 400 --n 20 --b 1000

**Why this has to exist before either variant is adopted.** Task 57 established
the current rule by running 400 no-signal campaigns and observing 0
acceptances. `identities=` (deduplicate identical looks) and `aligned=`
(Westfall-Young: one randomisation per replicate, shared across trials) are both
SHARPER than that rule by construction, and a sharper correction is only an
improvement if it still controls the error rate it exists to control. Preferring
the p-value it hands us is not evidence.

What is simulated, and what is not: this exercises the CORRECTION, not the
metric. Round turns are generated with symmetric gross -- no edge, by
construction -- so the observed assignment is exchangeable with every null
assignment and the single-trial p is uniform by exchangeability. The statistic
is a vectorised Sortino rather than `scorer.score_returns`, because 400
campaigns x 20 trials x 1,000 draws is eight million evaluations and the
fallback-to-Sharpe branch is not what these two flags touch. A variant that
fails here is disqualified; passing here is necessary and not sufficient.

**Trials are made DEPENDENT on purpose**, because that is the whole subject: a
fraction of each campaign's trials are exact duplicates of an earlier one, which
is what the record actually looks like (20 trials, 8 distinct blotters in
nq-s04-02; 100 and 46 in nq-s06-02).

## Measured 2026-10-26, q = 0.05, b = 1,000

    cell                            independent      dedup     aligned
    N=20, 35% dup, 16,000 camp.          0.0326     0.0486      0.0482
    N=50, 14% dup,  8,000 camp.          0.0409     0.0500      0.0481
    N=20, 60% dup,  8,000 camp.          0.0203     0.0491      0.0494
    N=400, 40% dup, 2,000 camp.          0.0280     0.0475      0.0490

The current rule is conservative in EVERY cell, and the more a campaign
duplicates the more conservative it gets -- 0.0203 at nq-s04-02's shape, which
is 40% of the error budget it is allowed to spend. Both variants sit on nominal
throughout, and neither upper CI bound reaches 0.06. `aligned` and
`aligned+dedup` accepted identical counts in every cell, which is alignment
subsuming deduplication, measured rather than argued.

`aligned` requires the observed and the nulls to share a coupling. They do: the
observed is the identity assignment -- flip nothing -- applied to every trial,
and null column j is one shared assignment applied to every trial. That is
exact Westfall-Young, and it is why `aligned` may NOT be read off nulls that
were seeded per trial.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.scoring.correction import max_of_n  # noqa: E402

MAR = 0.0


def _sortino(per_session: np.ndarray) -> np.ndarray:
    """Vectorised over the leading axis. `nan` where downside deviation is zero,
    which is the degenerate case the real scorer routes to Sharpe; those draws
    are dropped rather than given an invented value."""
    mean = per_session.mean(axis=-1)
    neg = np.minimum(per_session - MAR, 0.0)
    dd = np.sqrt((neg ** 2).mean(axis=-1))
    out = np.full(mean.shape, np.nan)
    ok = dd > 0
    out[ok] = (mean[ok] - MAR) / dd[ok]
    return out


def one_campaign(rng, n_trials, b, n_sessions, n_turns, dup_frac):
    """Observed best and per-trial nulls for one no-signal campaign."""
    distinct = max(1, int(round(n_trials * (1.0 - dup_frac))))
    protos = []
    for _ in range(distinct):
        gross = rng.normal(0.0, 1.0, size=n_turns)      # symmetric: NO edge
        cost = np.abs(rng.normal(0.05, 0.01, size=n_turns))
        slot = rng.integers(0, n_sessions, size=n_turns)
        protos.append((gross, cost, slot))
    pick = [protos[i % distinct] for i in range(n_trials)]
    rng.shuffle(pick)

    def per_session(signs, gross, cost, slot):
        """Scatter-add the round turns into sessions. Vectorised over replicates
        the same way `round_turn.null_from_turns` does it -- a Python loop over
        b rows here made the measurement too slow to run at a useful number of
        campaigns, which is its own kind of wrong answer."""
        contrib = np.atleast_2d(signs) * gross - cost
        c = contrib.shape[0]
        acc = np.zeros((c, n_sessions))
        np.add.at(acc, (np.arange(c)[:, None], slot[None, :]), contrib)
        return acc[0] if np.ndim(signs) == 1 else acc

    # ONE shared sign matrix: row j is replicate j, used by every trial.
    shared = rng.integers(0, 2, size=(b, n_turns), dtype=np.int8) * 2 - 1

    observed, nulls, ident = [], [], []
    for k, (gross, cost, slot) in enumerate(pick):
        obs_ps = per_session(np.ones(n_turns, dtype=np.int8), gross, cost, slot)
        o = _sortino(obs_ps)
        null_ps = per_session(shared, gross, cost, slot)
        v = _sortino(null_ps)
        v = v[~np.isnan(v)]
        if np.isnan(o) or v.size < 2:
            continue
        observed.append(float(o))
        nulls.append(v)
        ident.append(id(gross))          # exact duplicates share a prototype
    if not observed:
        return None
    return max(observed), nulls, ident


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaigns", type=int, default=400)
    ap.add_argument("--n", type=int, default=20, help="trials per campaign")
    ap.add_argument("--b", type=int, default=1000, help="null draws per trial")
    ap.add_argument("--sessions", type=int, default=105)
    ap.add_argument("--turns", type=int, default=150)
    ap.add_argument("--dup-frac", type=float, default=0.35,
                    help="fraction of trials that duplicate an earlier one")
    ap.add_argument("--q", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    rng = np.random.default_rng(a.seed)
    rules = {"independent": {}, "dedup": {}, "aligned": {}, "aligned+dedup": {}}
    counts = dict.fromkeys(rules, 0)
    ran = 0
    for c in range(a.campaigns):
        got = one_campaign(rng, a.n, a.b, a.sessions, a.turns, a.dup_frac)
        if got is None:
            continue
        obs, nulls, ident = got
        ran += 1
        for name in rules:
            kw = {}
            if "dedup" in name:
                kw["identities"] = ident
            if "aligned" in name:
                kw["aligned"] = True
            r = max_of_n(nulls, obs, b=a.b, q=a.q, **kw)
            counts[name] += int(r["reject"])
        if (c + 1) % 50 == 0:
            print(f"  {c+1}/{a.campaigns} campaigns ...", flush=True)

    print()
    print(f"  {ran} no-signal campaigns, N={a.n} trials, b={a.b}, "
          f"dup_frac={a.dup_frac}, q={a.q}")
    print(f"  a correction that controls FWER accepts <= {a.q:.0%} of these")
    print()
    print(f"  {'rule':16}{'accepts':>9}{'FWER':>9}{'95% CI':>18}")
    for name in rules:
        k = counts[name]
        f = k / ran if ran else float("nan")
        se = (f * (1 - f) / ran) ** 0.5 if ran else float("nan")
        lo, hi = max(0.0, f - 1.96 * se), min(1.0, f + 1.96 * se)
        flag = "" if hi <= a.q + 1e-9 or f <= a.q else "  <-- EXCEEDS q"
        print(f"  {name:16}{k:>9}{f:>9.4f}   [{lo:.4f}, {hi:.4f}]{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
