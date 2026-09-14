"""The per-candidate null, the profile-indexed null, and the max-of-N correction.

Task 30 measured what a single-strategy null costs: BH fired in 23 of 40
known-null campaigns against a promised 0.05. These pin the properties the
replacement has to have, and each states what its replication count can resolve
-- a check that cannot distinguish "passed" from "got lucky once" is not a check.
"""

from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.scoring.correction import (  # noqa: E402
    max_of_n, resolution_cap, step_down_max,
)
from src.core.scoring.randomization import candidate_null, rotate_session  # noqa: E402
from src.core.scoring.randomization.profile_null import (  # noqa: E402
    ProfileNull, bucket_of, profile_axes,
)
from src.core.scoring.randomization.rotation import RotatedDataset  # noqa: E402
from src.core.types import Split, Tick  # noqa: E402

SPEC = dict(n_sessions=175, ticks_per_session=390, vol_per_tick=0.15, seed=1)


# --------------------------------------------------------------------------- #
# what rotation preserves and destroys
# --------------------------------------------------------------------------- #
def test_rotation_preserves_the_increments_and_the_session_boundaries():
    """The design's claim, asserted on the real stream rather than on a toy.

    Preserved: tick count, timestamps, session labels, the multiset of
    within-session price increments, and each session's net move. Destroyed: the
    alignment between where in the session a move happened and the clock.
    """
    base = list(SyntheticDataset(SyntheticSpec(**SPEC)).ticks(Split.TRAIN))
    rot = list(RotatedDataset(SyntheticDataset(SyntheticSpec(**SPEC)),
                              seed=7).ticks(Split.TRAIN))

    assert len(base) == len(rot) > 10_000
    assert [t.ts for t in base] == [t.ts for t in rot]
    assert [t.session for t in base] == [t.session for t in rot]

    def increments(ticks):
        return sorted(round(b.price - a.price, 9)
                      for a, b in zip(ticks, ticks[1:]) if a.session == b.session)

    assert increments(base) == increments(rot), "the multiset of moves moved"

    def nets(ticks):
        out = {}
        for t in ticks:
            out.setdefault(t.session, [t.price, t.price])[1] = t.price
        return {k: round(v[1] - v[0], 9) for k, v in out.items()}

    assert nets(base) == nets(rot), "rotating increments must leave the sum alone"

    identical = sum(1 for a, b in zip(base, rot) if a.price == b.price)
    assert identical / len(base) < 0.3, (
        f"{identical / len(base):.1%} of prices unchanged; the rotation is not "
        "randomizing much"
    )


def test_a_constant_drift_rotates_to_itself():
    """The degenerate case, made explicit rather than discovered later. Equal
    increments are invariant under rotation, so a candidate sensitive only to the
    session's net move has no null distribution under this design."""
    ticks = [Tick(ts=i, price=100 + i * 0.25, size=1, session=1, seq=i)
             for i in range(20)]
    assert [t.price for t in rotate_session(ticks, 5)] == [t.price for t in ticks]


# --------------------------------------------------------------------------- #
# the resolution floor, which is the first thing about an empirical null
# --------------------------------------------------------------------------- #
def test_the_p_value_floor_is_reported_as_a_floor_and_never_substituted():
    """Task 30's two BH discoveries were partly manufactured by a clipped value
    used as a value: the grid bottomed at 0.0005 and two trials tied there, at
    the rank-2 BH critical value."""
    from src.core.scoring.randomization.candidate import CandidateNull

    result = CandidateNull(r=99, observed=10.0, null_scores=[0.0] * 99)
    assert result.p == pytest.approx(1 / 100)
    assert result.p_floor == pytest.approx(1 / 100)
    assert result.at_floor is True

    # and a merely good score is above the floor, not clipped to it
    mid = CandidateNull(r=99, observed=1.0,
                        null_scores=[0.0] * 94 + [2.0] * 5)
    assert mid.p == pytest.approx(6 / 100)
    assert mid.at_floor is False


def test_the_resolution_cap_is_the_arithmetic_the_brief_states():
    """N <= q(R+1). At q=0.05 and R=1000 that is 50, against an overnight run of
    21,600 trials -- four orders of magnitude, and no null closes it."""
    assert resolution_cap(99, 0.05) == 5
    assert resolution_cap(1_000, 0.05) == 50
    assert resolution_cap(10_000, 0.05) == 500


# --------------------------------------------------------------------------- #
# the max-of-N correction
# --------------------------------------------------------------------------- #
def test_max_of_n_does_not_reject_a_campaign_drawn_from_its_own_null():
    """Replication: 200 campaigns of 50 trials, so the realized rejection rate
    has binomial se 0.015 at the 0.05 target -- enough to resolve 0.05 from 0.10,
    which is the precision this property needs."""
    rng = np.random.default_rng(3)
    rejects = 0
    campaigns = 200
    for _ in range(campaigns):
        nulls = [rng.normal(0, 1, 300) for _ in range(50)]
        observed = [float(rng.normal()) for _ in range(50)]
        rejects += max_of_n(nulls, max(observed), b=2_000, q=0.05)["reject"]
    rate = rejects / campaigns
    se = math.sqrt(0.05 * 0.95 / campaigns)
    assert rate <= 0.05 + 3 * se, (rate, se)


def test_max_of_n_rejects_a_campaign_with_a_real_outlier():
    rng = np.random.default_rng(5)
    nulls = [rng.normal(0, 1, 300) for _ in range(50)]
    result = max_of_n(nulls, 6.0, b=10_000, q=0.05)
    assert result["reject"] and result["at_floor"]
    assert result["p"] == pytest.approx(1 / 10_001)


def test_max_of_n_gets_harder_as_the_campaign_grows():
    """The property the whole thing exists for: the same score is less impressive
    when it is the best of more attempts."""
    rng = np.random.default_rng(9)
    pool = [rng.normal(0, 1, 400) for _ in range(400)]
    small = max_of_n(pool[:10], 2.6, b=5_000)["p"]
    large = max_of_n(pool, 2.6, b=5_000)["p"]
    assert large > small * 3, (small, large)


def test_step_down_stops_at_the_first_non_rejection():
    rng = np.random.default_rng(11)
    nulls = [rng.normal(0, 1, 300) for _ in range(20)]
    observed = [6.0, 5.5] + [float(rng.normal()) for _ in range(18)]
    steps = step_down_max(nulls, observed, b=5_000, q=0.05)
    assert steps[0]["reject"] and steps[1]["reject"]
    assert not steps[-1]["reject"]
    assert len(steps) <= len(observed)


def test_a_trial_with_no_null_contributes_nothing_rather_than_a_placeholder():
    rng = np.random.default_rng(13)
    nulls = [rng.normal(0, 1, 200), [], rng.normal(0, 1, 200)]
    result = max_of_n(nulls, 1.0, b=1_000)
    assert result["n_trials"] == 2
    assert max_of_n([[], []], 1.0)["p"] == 1.0


# --------------------------------------------------------------------------- #
# the profile-indexed null
# --------------------------------------------------------------------------- #
def test_the_profile_axes_are_the_ones_the_metric_can_see():
    """Not dispersion: Sortino is mean/downside-deviation and both scale with the
    series, so scaling every return by a constant leaves the ratio unchanged. An
    index on dispersion would index on something the metric cannot see."""
    from src.core.scoring.metrics import sortino

    returns = [1.0, -2.0, 0.5, 0.0, -0.25, 3.0] * 8
    assert sortino(returns) == pytest.approx(sortino([10 * r for r in returns]))

    axes = profile_axes(returns)
    scaled = profile_axes([10 * r for r in returns])
    assert axes == scaled, "the axes must be scale-invariant too"


def test_a_thin_bucket_is_refused_rather_than_answered():
    table = ProfileNull(buckets={bucket_of(1.0, 0.5): [0.1] * 10})
    look = table.lookup([1.0, -1.0] * 30, 0.5)
    assert look["usable"] is False and look["p"] is None
    assert "not a tail" in look["reason"]


def test_the_committed_profile_table_reports_its_own_thin_buckets():
    table = ProfileNull.read()
    rows = table.summary()
    assert rows, "the committed profile null is empty"
    assert any(r["usable"] for r in rows)
    for row in rows:
        assert row["usable"] == (row["n"] >= 200)
        assert row["floor"] == pytest.approx(1 / (row["n"] + 1))


def test_the_bucket_edges_are_pinned_to_the_table():
    """A table tabulated on different edges is not this table, and reading it as
    if it were would silently mis-bucket every candidate."""
    import json

    doc = json.loads(
        (REPO_ROOT / "data" / "calibration" / "profile-null.json").read_text())
    from src.core.scoring.randomization import profile_null as pn

    assert tuple(doc["active_edges"]) == pn.ACTIVE_EDGES
    assert tuple(doc["downside_edges"]) == pn.DOWNSIDE_EDGES


# --------------------------------------------------------------------------- #
# end to end, small
# --------------------------------------------------------------------------- #
def test_a_candidate_null_runs_and_preserves_the_candidates_profile():
    """R=20 here, which puts the floor at 1/21 and can resolve nothing finer.
    That is fine for the property being asserted -- that the machinery runs and
    the behavioural profile survives -- and it is why the uniformity measurement
    lives in `docs/reviews/task31/` at R=99 over 60 candidates rather than here.
    """
    reference = (REPO_ROOT / "src" / "core" / "scoring" / "calibration"
                 / "reference.py")
    result = candidate_null(
        reference,
        lambda: SyntheticDataset(SyntheticSpec(
            n_sessions=60, ticks_per_session=120, vol_per_tick=0.15, seed=2)),
        class_name="ReferenceStrategy",
        r=20, split=Split.TRAIN, warmup_sessions=0)

    assert result.r == 20
    assert result.p_floor == pytest.approx(1 / 21)
    assert len(result.null_scores) == 20
    assert not result.degenerate
    drift = result.profile_drift
    assert drift, "no profile comparison was made"
    assert abs(drift["mean_position_size"]) < 0.25
    assert abs(drift["return_sd"]) < 0.6, drift


# --------------------------------------------------------------------------- #
# Task 32: the guarantee is conditional, and the FAR column is demoted
# --------------------------------------------------------------------------- #
def _flat(path: Path) -> str:
    """Prose with its line wrapping removed.

    These assertions are about what a document *says*, and a claim that moves
    because a paragraph was rewrapped -- or because someone put it in a
    blockquote -- is a test about formatting.
    """
    lines = [ln.lstrip("> ").rstrip() for ln in path.read_text().splitlines()]
    return " ".join(" ".join(lines).split())


def test_the_conditional_guarantee_is_stated_in_metric_md():
    """The decision, pinned where it was written down. A guarantee that lives
    only in a review document is a guarantee nobody quoting the metric will
    find."""
    text = _flat(REPO_ROOT / "docs" / "design" / "metric.md")
    assert "conditional on the observed training data" in text
    assert "not over which market happened" in text
    assert "does not extend to a period unlike the training data" in text


def test_the_far_column_says_it_is_not_the_accept_decision():
    """Demoted to screening in Task 32. The docstrings are where a caller finds
    out, so they are what the test reads."""
    import inspect

    from src.core.scoring.calibration import null as N

    for fn in (N.threshold_for, N.far_for):
        doc = inspect.getdoc(fn) or ""
        assert "screening" in doc.lower() or "diagnostic" in doc.lower(), fn
        flat = " ".join(doc.split())
        assert ("not the accept decision" in flat
                or "must not be the accept decision" in flat), fn


def test_the_two_misleading_numbers_are_flagged_where_they_appear():
    """`BH 0.005` and `Holm 0.000` came from a column that was 31.6% ones. They
    are quotable and wrong, so the warning has to sit beside them rather than in
    a later document."""
    text = _flat(REPO_ROOT / "docs" / "reviews" / "task31" / "README.md")
    assert "not successes and must not be quoted as any" in text
    assert "blind to unusual profiles" in text


def test_the_rotation_null_forbids_amortizing_children():
    """A candidate persisting across rotations could recognise repeated session
    sets and sandbag its own null. The fresh child per rotation is what the
    isolation multiplier buys, and amortizing is the obvious optimisation."""
    import inspect

    from src.core.scoring.randomization import candidate

    doc = inspect.getdoc(candidate.candidate_null) or ""
    assert "sandbag" in doc and "fresh child per rotation" in doc
    source = inspect.getsource(candidate.candidate_null)
    # the child is entered inside the per-run function, not hoisted out of it
    assert source.count("RemoteStrategy(") == 1
    assert "def run(dataset)" in source
    run_body = source.split("def run(dataset)")[1].split("t0 =")[0]
    assert "with RemoteStrategy(" in run_body, (
        "the child must be created per run, not shared across rotations"
    )


# --------------------------------------------------------------------------- #
# Task 33: the hybrid null, and the rule that keeps mixing two procedures valid
# --------------------------------------------------------------------------- #
def test_the_routing_rule_cannot_see_a_score():
    """*Mixing two valid procedures is valid when the choice between them is
    independent of the outcome, and invalid the moment it is not.*

    Enforced by the signature rather than by a convention: `choose_null` is given
    a return series and a table and has no parameter through which a score could
    reach it. A reviewer can check that in one line.
    """
    import inspect

    from src.core.scoring.randomization import choose_null

    params = list(inspect.signature(choose_null).parameters)
    assert params == ["returns", "table", "min_bucket"], params
    # the code, not the docstring -- the docstring is where the rule is
    # explained, so of course it says "score"
    source = inspect.getsource(choose_null)
    body = source.split('"""')[-1]
    for forbidden in ("score", "primary", "sortino", "observed"):
        assert forbidden not in body, (
            f"`{forbidden}` appears in the routing rule's body; the choice must "
            "depend on the profile alone"
        )


def test_routing_is_by_coverage_and_nothing_else():
    from src.core.scoring.randomization import choose_null
    from src.core.scoring.randomization.profile_null import (
        MIN_BUCKET_DEFAULT, ProfileNull, bucket_of, profile_axes,
    )

    returns = [1.0, -1.0] * 40 + [0.0] * 25
    axes = profile_axes(returns)
    key = bucket_of(axes["active"], axes["downside"])

    thin = ProfileNull(buckets={key: [0.0] * (MIN_BUCKET_DEFAULT - 1)})
    fat = ProfileNull(buckets={key: [0.0] * MIN_BUCKET_DEFAULT})
    assert choose_null(returns, thin).route == "rotation"
    assert choose_null(returns, fat).route == "bucket"
    # and the boundary is the parameter, not a hard-coded number
    assert choose_null(returns, thin, min_bucket=10).route == "bucket"


def test_the_conditional_table_is_built_from_runs_not_read_from_disk():
    """Under the conditional guarantee the null is 'other edgeless strategies
    with this profile *on this data*', so it is a per-campaign step. The
    committed `profile-null.json` is a screening artifact from Task 31, pooled
    across realizations, and is not the operating null."""
    import inspect

    from src.core.scoring.randomization.profile_null import ProfileNull

    table = ProfileNull.from_runs([
        ([1.0, -1.0] * 40 + [0.0] * 25, 0.3),
        ([1.0, -1.0] * 40 + [0.0] * 25, -0.1),
        ([1.0, -1.0] * 40 + [0.0] * 25, None),      # rejected, contributes nothing
    ])
    assert sum(len(v) for v in table.buckets.values()) == 2
    doc = inspect.getdoc(ProfileNull.from_runs) or ""
    assert "per-campaign step" in doc and "not the operating null" in doc


def test_the_expected_rotation_share_can_be_priced_before_a_campaign():
    """A campaign's cost is knowable in advance: the share is a property of the
    table's coverage and the generator's profile distribution, both known before
    the first trial."""
    from src.core.scoring.randomization import expected_rotation_share
    from src.core.scoring.randomization.profile_null import (
        MIN_BUCKET_DEFAULT, ProfileNull, bucket_of, profile_axes,
    )

    covered = [1.0, -1.0] * 40 + [0.0] * 25
    uncovered = [1.0] * 105
    key = bucket_of(**{k: v for k, v in profile_axes(covered).items() if k != "n"})
    table = ProfileNull(buckets={key: [0.0] * MIN_BUCKET_DEFAULT})
    share = expected_rotation_share(table, [covered] * 3 + [uncovered])
    assert share == pytest.approx(0.25)


def test_the_mixing_rule_is_written_where_the_metric_is_documented():
    text = _flat(REPO_ROOT / "docs" / "design" / "metric.md")
    assert ("Mixing two valid procedures is valid when the choice between them "
            "is independent of the outcome") in text
