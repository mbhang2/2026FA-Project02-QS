"""A null indexed by behavioural profile, not by one strategy.

Task 30 measured the cost of a single-strategy null: the reference gets 0/40 at
far <= 0.001, its variants get 10x. Task 31's per-candidate randomization fixes
that properly and costs R engine runs per candidate, which does not survive
campaign scale. This is the cheaper option the brief asked to be evaluated rather
than skipped, and it targets the measured cause directly.

**Which axes.** Not dispersion, despite dispersion being the headline number in
Task 30's diagnosis. Sortino is `mean / downside_deviation`, and both scale with
the series, so scaling all returns by a constant leaves the ratio unchanged: the
Sortino null is scale-invariant and a dispersion index would be indexing on
something the metric cannot see. Measured instead, over 24 edgeless variants:

    active fraction (sessions with a non-zero return)   0.343 .. 1.000
    downside fraction (sessions below the MAR)          0.162 .. 0.752
    dispersion                                          1.697 .. 5.507
    excess kurtosis                                    -0.090 .. 8.577

The reference strategy sits at active = 1.000, downside = 0.590 -- one corner of
that space. A candidate that trades in a third of the sessions has two thirds of
its return vector at exactly zero, which contributes nothing to the mean and
nothing to the downside deviation while still counting in the denominator. That
changes the shape of the Sortino distribution completely, and it is invisible to
a table tabulated at active = 1.

So the index is **(active fraction, downside fraction)**, both scale-invariant
and both where the variants actually spread.

**How it is built.** Not by simulating return series with prescribed
characteristics -- that would be a null of an assumed distribution rather than of
this generator. Many edgeless strategies are run over many independent data
realizations, each run is bucketed by its own measured profile, and the bucket's
pooled scores are its null. Every score in it comes from a strategy with no edge
on data with no signal, which is the definition being approximated.

**What it approximates away.** A candidate's null here is other strategies'
scores, not its own. Two candidates in the same bucket get the same null even if
they differ in ways the two axes do not capture. Whether that matters is a
measurement, not an opinion: `docs/reviews/task31/` compares the bucket null
against full per-candidate randomization on a sample of candidates.
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PATH = REPO_ROOT / "data" / "calibration" / "profile-null.json"

# Bucket edges on each axis. Coarse on purpose: finer buckets hold fewer scores,
# and the resolution of a bucket's tail is what sets the p-value floor. Reported
# per bucket so a thinly populated one is visible rather than silently unreliable.
ACTIVE_EDGES = (0.0, 0.5, 0.8, 0.95, 1.01)
DOWNSIDE_EDGES = (0.0, 0.35, 0.5, 0.65, 1.01)


def bucket_of(active: float, downside: float) -> str:
    a = bisect.bisect_right(ACTIVE_EDGES, active) - 1
    d = bisect.bisect_right(DOWNSIDE_EDGES, downside) - 1
    a = min(max(a, 0), len(ACTIVE_EDGES) - 2)
    d = min(max(d, 0), len(DOWNSIDE_EDGES) - 2)
    return f"a{a}d{d}"


def profile_axes(returns: Sequence[float], mar: float = 0.0) -> dict:
    """The two scale-invariant axes, from a return series."""
    n = len(returns)
    if n == 0:
        return {"active": 0.0, "downside": 0.0, "n": 0}
    return {
        "active": sum(1 for r in returns if r != 0.0) / n,
        "downside": sum(1 for r in returns if r < mar) / n,
        "n": n,
    }


@dataclass
class ProfileNull:
    """The tabulated buckets, and the lookup."""

    buckets: dict[str, list[float]] = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)

    def write(self, path: Path = DEFAULT_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"buckets": {k: sorted(v) for k, v in self.buckets.items()},
             "active_edges": list(ACTIVE_EDGES),
             "downside_edges": list(DOWNSIDE_EDGES),
             "provenance": self.provenance},
            indent=1, sort_keys=True) + "\n")

    @classmethod
    def read(cls, path: Path = DEFAULT_PATH) -> "ProfileNull":
        doc = json.loads(path.read_text())
        if (tuple(doc["active_edges"]) != ACTIVE_EDGES
                or tuple(doc["downside_edges"]) != DOWNSIDE_EDGES):
            raise ValueError(
                f"{path} was tabulated on different bucket edges than the code "
                "expects; re-tabulate rather than reading it")
        return cls(buckets={k: list(v) for k, v in doc["buckets"].items()},
                   provenance=doc.get("provenance", {}))

    def lookup(self, returns: Sequence[float], observed: float) -> dict:
        """A candidate's p-value from its bucket, with the floor reported.

        A bucket with fewer than 200 scores is refused rather than answered: its
        tail is a handful of points and a p-value from it would be a number with
        no measurement behind it.
        """
        axes = profile_axes(returns)
        key = bucket_of(axes["active"], axes["downside"])
        sample = self.buckets.get(key, [])
        floor = 1.0 / (len(sample) + 1) if sample else 1.0
        if len(sample) < 200:
            return {"bucket": key, "axes": axes, "n_in_bucket": len(sample),
                    "p": None, "p_floor": floor, "usable": False,
                    "reason": f"bucket {key} holds {len(sample)} scores; "
                              "fewer than 200 is not a tail"}
        exceed = sum(1 for s in sample if s >= observed)
        return {
            "bucket": key, "axes": axes, "n_in_bucket": len(sample),
            "p": (1 + exceed) / (len(sample) + 1),
            "p_floor": floor,
            "at_floor": (1 + exceed) / (len(sample) + 1) <= floor + 1e-12,
            "usable": True,
            "bucket_mean": statistics.fmean(sample),
            "bucket_sd": statistics.stdev(sample) if len(sample) > 1 else 0.0,
        }

    def sample_for(self, returns: Sequence[float]) -> list[float]:
        """The bucket's scores, for feeding `correction.max_of_n`."""
        axes = profile_axes(returns)
        return list(self.buckets.get(bucket_of(axes["active"], axes["downside"]), []))

    def summary(self) -> list[dict]:
        out = []
        for key in sorted(self.buckets):
            v = self.buckets[key]
            out.append({
                "bucket": key, "n": len(v),
                "mean": statistics.fmean(v) if v else None,
                "sd": statistics.stdev(v) if len(v) > 1 else None,
                # the upper tail, which is the end a candidate has to beat;
                # `v` is not sorted in memory, only on the way to disk
                "q95": sorted(v)[int(len(v) * 0.95)] if v else None,
                "floor": 1.0 / (len(v) + 1) if v else 1.0,
                "usable": len(v) >= 200,
            })
        return out
