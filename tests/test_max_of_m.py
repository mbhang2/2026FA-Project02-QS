"""The L2 gate: the best of M campaign winners, on validation.

`max_of_m` was named as the pass condition in three places and implemented in
none. Its absence was the real blocker on running many campaigns: each campaign
corrects for its own trials, nothing corrected for the number of campaigns, so a
loop that produced campaigns faster made the accounting worse rather than better.

Four properties carry the weight, and three of them are about refusing to flatter
the result:

1. Every campaign enters the family, including the ones that did badly.
2. Rule (a) is separate from rule (b), because the sign-flip null tests
   directional skill and a losing candidate can still beat it.
3. An incomplete family gets no verdict, because a correction over an unknown
   family size is not a correction.
4. It is the same simulation as `max_of_n`, deliberately, so the L2 gate inherits
   an error rate that has already been measured.
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.scoring.correction import max_of_m, max_of_n  # noqa: E402


def _winners(scores, *, r=200, sd=0.1, seed=3, nulls=None):
    """M campaign winners with nulls drawn from a known normal."""
    rng = np.random.default_rng(seed)
    out = []
    for i, s in enumerate(scores):
        null = (list(nulls[i]) if nulls is not None
                else rng.normal(0.0, sd, r).tolist())
        out.append({"campaign_id": f"c{i}", "validation_primary": float(s),
                    "validation_null": null})
    return out


# --------------------------------------------------------------------------- #
# 1. the family is every campaign, not the good ones
# --------------------------------------------------------------------------- #
def test_the_weak_winners_stay_in_the_family():
    """The multiplicity comes from having looked M times, not from having looked
    M times and liked the answer. Dropping the losers shrinks the family and
    inflates the maximum's significance -- the same defect as excluding crashed
    trials from N."""
    strong = _winners([0.30])
    with_weak = _winners([0.30, -0.10, -0.25, -0.40, 0.02])

    assert with_weak[0]["validation_primary"] == 0.30
    a, b = max_of_m(strong), max_of_m(with_weak)
    assert a["m_campaigns"] == 1 and b["m_campaigns"] == 5
    assert b["campaigns"] == ["c0", "c1", "c2", "c3", "c4"]
    # five looks make the same observation less surprising than one look
    assert b["p"] > a["p"], (a["p"], b["p"])
    assert b["null_max_mean"] > a["null_max_mean"]


def test_a_campaign_is_never_silently_dropped():
    """Contrast with max_of_n, which skips a trial whose null is empty. There an
    empty null is a property of a candidate that never traded; here a missing
    null is a gap in our own work, and quietly shrinking M would be a correction
    over the campaigns that happened to be ready."""
    w = _winners([0.30, 0.10])
    w[1]["validation_null"] = []
    out = max_of_m(w)
    assert out["m_campaigns"] == 2, "M counts the campaigns we ran"
    assert out["incomplete"] == ["c1"]
    assert out["verdict"] is None and out["p"] is None
    assert "unknown family size" in out["withheld"]

    # and max_of_n, on the same shape, does drop it -- the asymmetry is intended
    n = max_of_n([w[0]["validation_null"], []], 0.30)
    assert n["n_trials"] == 1


# --------------------------------------------------------------------------- #
# 2. rule (a) is not rule (b)
# --------------------------------------------------------------------------- #
def test_a_losing_winner_fails_even_when_the_null_is_beaten():
    """**The case rule (a) exists for.** Section 3b found a candidate losing 3,275
    points scoring p = 0.0005 against its own null, because the sign-flip null
    tests directional skill rather than profitability. A small p with a losing
    candidate must not read as a pass."""
    # **The real mechanism, not a contrived one.** A losing candidate beats its
    # own null because sign-flipping its round turns makes it lose MORE: the null
    # is centred well below zero, so -0.05 sits far above it while still being a
    # loss. That is how a candidate down 3,275 points reached p = 0.0005.
    rng = np.random.default_rng(1)
    low = rng.normal(-0.30, 0.02, 400).tolist()
    w = _winners([-0.05, -0.20], nulls=[low, low])
    assert max(low) < -0.05, "the null must sit below the observed loss"
    out = max_of_m(w)
    assert out["reject"] is True, "rule (b) is satisfied"
    assert out["rule_a_pass"] is False, "rule (a) is not"
    assert out["passes"] is False and out["verdict"] == "FAIL"
    assert "directional skill, not profitability" in out["note"]


def test_rule_a_is_reported_for_every_winner_not_only_the_best():
    """Section 5: computed for each of the M winners, all three reported."""
    out = max_of_m(_winners([0.05, -0.02, 0.31]))
    assert out["rule_a_sortino_positive"] == {"c0": True, "c1": False, "c2": True}
    assert out["best_campaign"] == "c2"
    assert out["rule_a_pass"] is True, "the candidate on trial is the best one"


def test_both_conditions_are_required():
    tight = np.random.default_rng(2).normal(0.0, 0.01, 400).tolist()
    # (a) passes, (b) fails: positive but unremarkable against a wide null
    wide = np.random.default_rng(5).normal(0.0, 1.0, 400).tolist()
    out = max_of_m(_winners([0.05], nulls=[wide]))
    assert out["rule_a_pass"] is True and out["reject"] is False
    assert out["passes"] is False and out["verdict"] == "FAIL"
    # both pass
    out = max_of_m(_winners([0.30], nulls=[tight]))
    assert out["rule_a_pass"] and out["reject"] and out["verdict"] == "PASS"


# --------------------------------------------------------------------------- #
# 3. it is the same machinery, on purpose
# --------------------------------------------------------------------------- #
def test_it_is_max_of_n_with_the_campaign_as_the_unit():
    """Section 4 chooses max_of_m over a Holm budget because it uses the actual
    null distribution of the maximum, and says the L2 gate inherits the error rate
    already measured for that machinery rather than needing its own calibration.
    A second implementation of the same simulation would be a second thing to
    calibrate, so the p must be identical."""
    w = _winners([0.05, -0.02, 0.31])
    m = max_of_m(w)
    n = max_of_n([x["validation_null"] for x in w], 0.31)
    assert m["p"] == n["p"]
    assert m["null_max_mean"] == n["null_max_mean"]
    assert m["null_max_quantiles"] == n["null_max_quantiles"]

    from src.core.scoring import correction
    assert "inherits" in correction.MAX_OF_M_NOTE.lower()
    assert "0.060" in correction.MAX_OF_M_NOTE, "it names the rate it inherits"


def test_the_error_control_holds_on_a_known_null():
    """The property the whole gate rests on: under the null, the pass rate is at
    or below q. 600 synthetic L2 families of five campaigns each, every winner
    drawn from the same distribution as its own null."""
    rng = np.random.default_rng(11)
    q, trials, rejects = 0.05, 600, 0
    for _ in range(trials):
        w = []
        for i in range(5):
            null = rng.normal(0.0, 0.1, 200)
            # the winner is one more draw from the same law -- no edge
            w.append({"campaign_id": f"c{i}",
                      "validation_primary": float(rng.normal(0.0, 0.1)),
                      "validation_null": null.tolist()})
        rejects += bool(max_of_m(w, b=2_000, q=q, seed=int(rng.integers(1e6)))["reject"])
    rate = rejects / trials
    assert rate <= 0.09, f"rejected {rate:.3f} of null families at q={q}"


# --------------------------------------------------------------------------- #
# 4. what it will not do
# --------------------------------------------------------------------------- #
def test_it_does_not_decide_promotion():
    """No verdict in this project may mean approval. §5 is a pass rule; promotion
    beyond the MVP is a human decision on all of the evidence."""
    out = max_of_m(_winners([0.30], nulls=[
        np.random.default_rng(4).normal(0.0, 0.01, 400).tolist()]))
    assert out["verdict"] == "PASS"
    for word in ("promote", "promotion", "approve", "approved"):
        assert word not in str(out).lower().replace("promotion beyond", "")
    import inspect
    from src.core.scoring import correction
    doc = inspect.getdoc(correction.max_of_m)
    assert "does not decide promotion" in doc


def test_it_does_not_pretend_to_check_the_stopping_rule():
    """§4's stopping rule -- a human may stop L2 for any reason that is not a
    validation result -- is not checkable from these inputs. The protocol's
    defence is structural, not arithmetic, and the docstring says so rather than
    implying the function guards it."""
    import inspect
    from src.core.scoring import correction
    doc = inspect.getdoc(correction.max_of_m)
    assert "is not checked" in doc and "stopping rule" in doc


def test_an_empty_family_is_not_a_pass():
    out = max_of_m([])
    assert out["m_campaigns"] == 0 and out["verdict"] is None


@pytest.mark.parametrize("m", [1, 2, 5, 12])
def test_the_family_size_is_recorded_however_large(m):
    out = max_of_m(_winners([0.1] * m))
    assert out["m_campaigns"] == m and len(out["campaigns"]) == m


# --------------------------------------------------------------------------- #
# input types the gate must not raise on
# --------------------------------------------------------------------------- #
def test_a_numpy_null_sample_is_accepted_not_ambiguous():
    """`arr or ()` raises on any array with more than one element, and that is
    numpy refusing an ambiguity rather than a numpy quirk to work around.

    `round_turn.null_sample` returns a list, so `l2_gate.py` never reached the
    branch -- Task 70's simulation passes arrays and hit it on its first run. A
    gate that raises on a legitimate input type fails whenever somebody new calls
    it, which is the case the L2 gate exists to serve.
    """
    import numpy as np
    winners = [
        {"campaign_id": "a", "validation_primary": 0.9,
         "validation_null": np.linspace(-1.0, 1.0, 200)},
        {"campaign_id": "b", "validation_primary": 0.2,
         "validation_null": np.linspace(-1.0, 1.0, 200)},
    ]
    out = max_of_m(winners, b=500, seed=3)
    assert out["incomplete"] == []
    assert out["verdict"] in ("PASS", "FAIL")
    assert out["m_campaigns"] == 2
    assert out["best_campaign"] == "a"


def test_an_empty_numpy_null_still_counts_as_incomplete():
    """The array fix must not turn a missing null into a silent zero-length pass."""
    import numpy as np
    winners = [
        {"campaign_id": "a", "validation_primary": 0.9,
         "validation_null": np.linspace(-1.0, 1.0, 200)},
        {"campaign_id": "b", "validation_primary": 0.2,
         "validation_null": np.array([])},
    ]
    out = max_of_m(winners, b=500, seed=3)
    assert out["incomplete"] == ["b"]
    assert out["verdict"] is None


# --------------------------------------------------------------------------- #
# Task 70: the raw maximum is valid but blind
# --------------------------------------------------------------------------- #
def _real_shaped(rng, primaries, moments, draws=300):
    """Winners whose nulls have our six campaigns' real locations and scales."""
    return [{"campaign_id": cid, "validation_primary": v,
             "validation_null": (m + s * rng.standard_normal(draws)).tolist()}
            for (cid, m, s), v in zip(moments, primaries)]


def test_the_raw_and_standardised_statistics_are_both_always_reported():
    """The verdict follows one statistic; the record carries both, so the other
    never needs a rerun to be known."""
    import numpy as np
    w = _real_shaped(np.random.default_rng(9), [-0.15, -0.055],
                     [("tight", -0.8934, 0.0416), ("wide", -0.0703, 0.1454)])
    out = max_of_m(w, b=2000, seed=5)
    assert out["statistic"] == "raw", "the pre-registered default"
    assert "standardised_view" in out and "raw_view" in out
    assert out["raw_view"]["p"] == out["p"], "raw is in force by default"
    assert out["standardised_view"]["p"] != out["p"], (
        "on these moments the two statistics must disagree, or the fixture has "
        "stopped exercising the finding")


def test_per_campaign_records_each_campaigns_distance_above_its_own_null():
    """`nq-orderflow-01` sits +17.8 sds above its own validation null and lost the
    best-of-M contest to a campaign at +0.1. That number was not in any output
    before Task 70, which is why nobody saw it."""
    import numpy as np
    w = _real_shaped(np.random.default_rng(9), [-0.15, -0.055],
                     [("tight", -0.8934, 0.0416), ("wide", -0.0703, 0.1454)])
    out = max_of_m(w, b=500, seed=5)
    z = {c["campaign_id"]: c["sds_above_own_null"] for c in out["per_campaign"]}
    assert z["tight"] > 10, z
    assert abs(z["wide"]) < 1, z
    for c in out["per_campaign"]:
        assert c["null_sd"] > 0 and "null_mean" in c


def test_the_raw_statistic_names_a_disagreement_rather_than_hiding_it():
    """A gate that quietly differs from a better-calibrated one is worse than
    either. The verdict does not move -- the statistic in force is a
    pre-registration decision -- but the record says so."""
    import numpy as np
    w = _real_shaped(np.random.default_rng(9), [-0.15, -0.055],
                     [("tight", -0.8934, 0.0416), ("wide", -0.0703, 0.1454)])
    out = max_of_m(w, b=2000, seed=5)
    assert out["standardised_view"]["reject"] and not out["reject"]
    assert "disagreement" in out
    assert "valid but blind" in out["disagreement"]
    assert "recorded, not acted on" in out["disagreement"]


def test_the_standardised_statistic_judges_rule_a_on_its_own_winner():
    """Rule (b) on one campaign and rule (a) on a different one, reported as one
    verdict, would be two rules about two candidates."""
    import numpy as np
    w = _real_shaped(np.random.default_rng(9), [-0.15, -0.055],
                     [("tight", -0.8934, 0.0416), ("wide", -0.0703, 0.1454)])
    out = max_of_m(w, b=2000, seed=5, statistic="standardised")
    assert out["best_campaign"] == "tight"
    assert out["observed_best"] == pytest.approx(-0.15)
    assert out["rule_a_pass"] is False, "-0.15 is not above zero"
    assert out["verdict"] == "FAIL", "rule (a) still has to pass"
    assert out["raw_view"]["best_campaign"] == "wide", (
        "raw_view must report the RAW winner even when standardised is in force")


def test_the_family_does_not_shrink_when_a_null_cannot_be_standardised():
    """A degenerate null contributes nothing to the standardised maximum, exactly
    as an empty null does in max_of_n -- but M is unchanged, because M is how many
    times we looked."""
    import numpy as np
    rng = np.random.default_rng(4)
    w = [{"campaign_id": "flat", "validation_primary": 0.5,
          "validation_null": [0.2] * 200},
         {"campaign_id": "normal", "validation_primary": 0.1,
          "validation_null": rng.standard_normal(200).tolist()}]
    out = max_of_m(w, b=500, seed=5, statistic="standardised")
    assert out["m_campaigns"] == 2
    assert out["incomplete"] == []
    flat = [c for c in out["per_campaign"] if c["campaign_id"] == "flat"][0]
    assert flat["null_sd"] == 0.0
    assert flat["sds_above_own_null"] is None
    assert out["best_campaign"] == "normal"


def test_an_unknown_statistic_is_refused():
    import numpy as np
    w = _real_shaped(np.random.default_rng(1), [0.1, 0.2],
                     [("a", 0.0, 1.0), ("b", 0.0, 1.0)])
    with pytest.raises(ValueError, match="pre-registered default"):
        max_of_m(w, b=100, seed=1, statistic="z")
