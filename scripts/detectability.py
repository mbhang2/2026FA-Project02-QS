#!/usr/bin/env python3
"""What size of edge could this gate have found? Power, from our own nulls.

    python scripts/detectability.py --train
    python scripts/detectability.py --validation
    python scripts/detectability.py --l2 nq-s07-01 nq-s07-02 nq-s07-03

"We ran 2,632 trials on NQ and found nothing" is not a result until somebody
says what we could have found. A gate that detects nothing because no edge
exists and a gate that detects nothing because it cannot detect anything look
identical from the outside, and only this distinguishes them.

## It uses the project's own nulls, not a distribution somebody assumed

Every campaign already stores the sign-flip null the gate was judged against --
`nulls.json` holds one 10,000-draw sample per candidate, `validation-null.json`
one for the winner on the held-out split. The critical value is read straight
off those: the score an observation must beat for `p <= q`. Nothing here is
Gaussian, nothing is fitted, and if the nulls are wrong then the gate was
already wrong in the same way.

## What "a true edge of d" means

`round_turn_mc` flips the sign of each round turn, so a candidate's null is its
own score with the direction of its calls randomised. An edge of `d` is
therefore **d Sortino better than that candidate would do with random
direction**, which is the thing the gate is actually testing for.

## Four honest limits, stated because they bound the answer

1. **It is a lower bound on power.** Exactly one candidate is given the edge and
   the rest stay null. If a real signal lifted several candidates at once, the
   gate would find it more easily than this says.
2. **The representative candidate is the one with median null dispersion.**
   Dispersion varies a lot -- from 0.0075 to 1.79 across this project's
   campaigns -- so a single number hides a wide spread, and the per-campaign
   table is the honest form.
3. **A shift leaves the null's shape alone.** A strategy with a real edge might
   also be less erratic, which this cannot model.
4. **The false-positive floor is in the number.** Power never goes below `q`,
   because the other candidates can clear the bar by luck at exactly that rate.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
CAMPAIGNS = ROOT / "runs" / "campaigns"
Q = 0.05
POWER = 0.80


def _fmt(x: float) -> str:
    """An unreachable bar is said, not printed as a big number."""
    return "unreachable" if x == float("inf") else f"{x:.3f}"


def _np():
    import numpy
    return numpy


def load_train_null(campaign: str):
    """(matrix, draws_per_candidate, candidates_dropped) or None."""
    np = _np()
    p = CAMPAIGNS / campaign / "nulls.json"
    if not p.exists():
        return None
    raw = [s for s in (json.loads(p.read_text()).get("samples") or []) if s]
    if len(raw) < 5:
        return None
    # A crashed or unscored trial has an empty sample and is already absent;
    # a *short* one would make the matrix ragged, so keep the modal width and
    # report what that cost rather than silently reshaping.
    b, _ = Counter(len(s) for s in raw).most_common(1)[0]
    keep = [s for s in raw if len(s) == b]
    return np.asarray(keep, dtype=float), b, len(raw) - len(keep)


def minimum_detectable(null, critical: float, q: float = Q,
                       power: float = POWER) -> float:
    """The smallest shift of `null` that clears `critical` with `power`.

    **The bracket is found before it is bisected**, which the first version of
    this did not do: it bisected a fixed `(0, 5]` and returned `5.0` whenever
    the true answer was larger. `nq-s04-03`'s null has sd 1.79, so its real
    requirement is far past five -- and the table printed a confident `5.000`
    for it, a capped search reported as a measurement. A test on a null ten
    times as wide is what found it.
    """
    def achieved(d: float) -> float:
        return q + (1 - q) * float((null + d > critical).mean())

    if achieved(0.0) >= power:
        return 0.0
    hi = 1.0
    while achieved(hi) < power:
        hi *= 2.0
        if hi > 1e6:
            return float("inf")      # no finite edge reaches this bar
    # **Bisect from zero, not from `hi/2`.** Setting the floor to half the
    # bracket assumed the search had doubled at least once; for a narrow null
    # the first `hi` already clears the bar and the true answer is below that
    # floor, so a campaign with sd 0.05 came back as 0.5. Found by a test that
    # only asked for the answer to scale with the dispersion.
    lo = 0.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if achieved(mid) >= power else (mid, hi)
    return hi


def _representative(matrix):
    """The candidate with median null dispersion -- see limit 2."""
    np = _np()
    return matrix[np.argsort(matrix.std(axis=1))[matrix.shape[0] // 2]]


def train_row(campaign: str) -> dict | None:
    np = _np()
    got = load_train_null(campaign)
    if got is None:
        return None
    matrix, b, dropped = got
    critical = float(np.quantile(matrix.max(axis=0), 1 - Q))
    summary = CAMPAIGNS / campaign / "summary.json"
    observed = None
    if summary.exists():
        observed = (json.loads(summary.read_text()).get("gate") or {}).get("observed_best")
    return {"campaign": campaign, "candidates": int(matrix.shape[0]), "b": b,
            "ragged_dropped": dropped, "critical": critical,
            "null_sd_median": float(np.median(matrix.std(axis=1))),
            "mde": minimum_detectable(_representative(matrix), critical),
            "observed_best": observed}


def validation_row(campaign: str) -> dict | None:
    np = _np()
    p = CAMPAIGNS / campaign / "validation-null.json"
    if not p.exists():
        return None
    sample = np.asarray(json.loads(p.read_text()).get("sample") or [], dtype=float)
    if sample.size < 100:
        return None
    critical = float(np.quantile(sample, 1 - Q))
    v = CAMPAIGNS / campaign / "validation.json"
    observed = None
    if v.exists():
        observed = (json.loads(v.read_text()).get("validation") or {}).get("primary")
    return {"campaign": campaign, "draws": int(sample.size),
            "null_mean": float(sample.mean()), "null_sd": float(sample.std()),
            "critical": critical, "mde": minimum_detectable(sample, critical),
            "observed": observed}


def l2_row(campaigns: list[str]) -> dict | None:
    """`max_of_m` over a DECLARED family -- the bar the whole session must clear."""
    np = _np()
    samples, used = [], []
    for c in campaigns:
        p = CAMPAIGNS / c / "validation-null.json"
        if not p.exists():
            continue
        s = np.asarray(json.loads(p.read_text()).get("sample") or [], dtype=float)
        if s.size >= 100:
            samples.append(s); used.append(c)
    if len(samples) < 2:
        return None
    width = min(s.size for s in samples)
    stack = np.vstack([s[:width] for s in samples])
    critical = float(np.quantile(stack.max(axis=0), 1 - Q))
    return {"family": used, "m": len(used), "critical": critical,
            "mde": minimum_detectable(_representative(stack), critical)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--validation", action="store_true")
    ap.add_argument("--l2", nargs="+", metavar="CAMPAIGN")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not (a.train or a.validation or a.l2):
        ap.error("give --train, --validation or --l2")

    out: dict = {"q": Q, "power": POWER}
    names = sorted(p.name for p in CAMPAIGNS.iterdir() if p.is_dir())

    if a.train:
        rows = [r for r in (train_row(n) for n in names) if r]
        rows.sort(key=lambda r: r["candidates"])
        out["train"] = rows
        if not a.json:
            print(f"TRAIN -- max_of_n, q={Q}, power={POWER:.0%}\n")
            print(f"{'campaign':22} {'N':>5} {'critical':>9} {'null sd':>8} "
                  f"{'MDE':>7} {'observed':>9}")
            for r in rows:
                o = (f"{r['observed_best']:+.4f}"
                     if isinstance(r["observed_best"], float) else "—")
                print(f"{r['campaign']:22} {r['candidates']:>5} "
                      f"{r['critical']:>+9.4f} {r['null_sd_median']:>8.4f} "
                      f"{_fmt(r['mde']):>7} {o:>9}")

    if a.validation:
        rows = [r for r in (validation_row(n) for n in names) if r]
        out["validation"] = rows
        if not a.json:
            print(f"\nVALIDATION -- one winner against its own null, q={Q}\n")
            print(f"{'campaign':22} {'null sd':>8} {'critical':>9} {'MDE':>7} "
                  f"{'observed':>9}")
            for r in rows:
                o = (f"{r['observed']:+.4f}"
                     if isinstance(r["observed"], float) and abs(r["observed"]) != float("inf")
                     else "—")
                print(f"{r['campaign']:22} {r['null_sd']:>8.4f} "
                      f"{r['critical']:>+9.4f} {r['mde']:>7.3f} {o:>9}")

    if a.l2:
        r = l2_row(a.l2)
        out["l2"] = r
        if not a.json:
            if r is None:
                print("\nL2: fewer than two of those campaigns have a validation null.")
            else:
                print(f"\nL2 -- max_of_m over a declared family of {r['m']}\n")
                print(f"  family   {', '.join(r['family'])}")
                print(f"  critical {r['critical']:+.4f}")
                print(f"  MDE      {_fmt(r['mde'])}")

    if a.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
