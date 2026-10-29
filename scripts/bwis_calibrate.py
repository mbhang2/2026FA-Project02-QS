#!/usr/bin/env python3
"""Calibrate source priors from the paper registry. No gateway call.

    .venv/bin/python scripts/bwis_calibrate.py
    .venv/bin/python scripts/bwis_calibrate.py --check

Counts, per source, how many screened papers turned out usable
(`feasibility in {direct, adaptable}`) and writes `config/bwis-priors.json`.

**Why this is calibratable with the gateway dead.** Every `feasibility` verdict
in the registry was already paid for by a screening pass. 532 of 547 papers
carry one. Recounting them is arithmetic.

`--check` recalibrates and refuses if the committed file disagrees, which is
the CI form: a registry that gained papers and a priors file that did not are
exactly the pair that drifts apart silently.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.research import bwis  # noqa: E402

REGISTRY = ROOT / "data" / "papers" / "registry.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--registry", type=Path, default=REGISTRY)
    ap.add_argument("--out", type=Path, default=bwis.CONFIG_PATH)
    ap.add_argument("--check", action="store_true",
                    help="recalibrate and refuse if the committed file differs")
    a = ap.parse_args()

    papers = list(json.loads(a.registry.read_text())["papers"].values())
    cal = bwis.calibrate(papers)

    print(f"calibrated from {a.registry.relative_to(ROOT)}: "
          f"{cal.n_papers} screened papers, baseline {cal.baseline:.4f}")
    print(f"\n{'cell':<14}{'n':>6}{'usable':>8}{'mean':>8}{'conc':>8}"
          f"{'80% interval':>18}{'band':>7}")
    rows = sorted(cal.by_source.items(), key=lambda kv: -kv[1].mean)
    for name, p in rows + [("GLOBAL", cal.global_prior)]:
        lo, hi = p.interval()
        print(f"{name:<14}{p.n_observed:>6}{p.alpha - bwis.PRIOR_HITS:>8.0f}"
              f"{p.mean:>8.3f}{p.concentration:>8.1f}"
              f"{f'[{lo:.3f}, {hi:.3f}]':>18}"
              f"{bwis.band(p, baseline=cal.baseline):>7}")

    if a.check:
        if not a.out.is_file():
            sys.exit(f"{a.out} does not exist; run without --check")
        stored = json.loads(a.out.read_text())
        stored.pop("digest", None)
        if stored != cal.as_dict():
            sys.exit(f"{a.out.relative_to(ROOT)} is stale against the registry; "
                     f"re-run without --check")
        print(f"\n{a.out.relative_to(ROOT)} matches the registry")
        return 0

    digest = bwis.write(a.out, cal)
    print(f"\nwrote {a.out.relative_to(ROOT)}  digest {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
