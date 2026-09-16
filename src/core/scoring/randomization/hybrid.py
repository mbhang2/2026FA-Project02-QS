"""Bucket when covered, rotation when not. The selection keys on profile only.

Task 32's conditional gate controls -- FWER 0.045, CI [0.029, 0.070] -- and
21.8% of candidates never reach it. A candidate whose behavioural profile lands
in a thin bucket has no null, enters the correction at p = 1, and can never be a
discovery. The measured 0.045 is conservative *because of that gap*.

**It is not only a power loss.** The gap is in the tails of profile space, where
unusual strategies live. Once a real agent is hill-climbing, novel profiles are
silently unacceptable, so the loop drifts toward the region the gate can see --
and the calibration's coverage becomes an implicit prior over strategy space,
imposed by nobody and documented nowhere.

**The selection rule keys on the profile and only on the profile, and is fixed
before the campaign runs.** Never on the score, never on which null gives the
nicer answer.

*Mixing two valid procedures is valid when the choice between them is independent
of the outcome, and invalid the moment it is not.*

That sentence is in `docs/design/metric.md` as well, and the rule is recorded in
the campaign record so it is auditable rather than remembered. `choose_null`
takes the profile axes and nothing else -- it cannot see a score, because it is
not given one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .profile_null import MIN_BUCKET_DEFAULT, ProfileNull, bucket_of, profile_axes


@dataclass(frozen=True, slots=True)
class NullChoice:
    """Which null a candidate gets, and why. Decided from the profile alone."""

    route: str                 # "bucket" | "rotation"
    bucket: str
    n_in_bucket: int
    axes: dict

    def as_dict(self) -> dict:
        return {"route": self.route, "bucket": self.bucket,
                "n_in_bucket": self.n_in_bucket, "axes": self.axes}


def choose_null(returns: Sequence[float], table: ProfileNull, *,
                min_bucket: int = MIN_BUCKET_DEFAULT) -> NullChoice:
    """The routing decision. Takes a return series; **is never given a score.**

    Not passing the score is the enforcement, not a convention: a function that
    cannot see the outcome cannot select on it. `tests/test_randomization.py`
    asserts the signature has no way to take one.
    """
    axes = profile_axes(returns)
    key = bucket_of(axes["active"], axes["downside"])
    n = len(table.buckets.get(key, ()))
    return NullChoice(route="bucket" if n >= min_bucket else "rotation",
                      bucket=key, n_in_bucket=n, axes=axes)


def expected_rotation_share(table: ProfileNull, sample_returns, *,
                            min_bucket: int = MIN_BUCKET_DEFAULT) -> float:
    """What fraction of a campaign will need the expensive fallback.

    For pricing a campaign before running it, from a sample of profiles. The
    share is a property of the table's coverage and the generator's profile
    distribution, both of which are known in advance.
    """
    choices = [choose_null(r, table, min_bucket=min_bucket) for r in sample_returns]
    if not choices:
        return 0.0
    return sum(1 for c in choices if c.route == "rotation") / len(choices)
