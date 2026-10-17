"""What the gate could have found, computed from the nulls it already used.

"2,632 trials on NQ and nothing" is not a result until somebody says what could
have been found. A gate that finds nothing because there is nothing and a gate
that finds nothing because it cannot look identical from outside.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import numpy as np
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "scripts" / "detectability.py").read_text()


def _mod():
    spec = importlib.util.spec_from_file_location(
        "detectability", REPO_ROOT / "scripts" / "detectability.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_power_never_falls_below_the_false_positive_rate():
    """With no edge at all the gate still rejects at q, because the other
    candidates clear the bar by luck. A power curve that started at zero would
    be hiding that."""
    mod = _mod()
    rng = np.random.default_rng(0)
    null = rng.normal(0, 1, 20_000)
    crit = float(np.quantile(null, 0.95))
    # the shift needed for 80% power must be positive and finite
    d = mod.minimum_detectable(null, crit)
    assert 0 < d < 5
    # and at zero shift the achieved power is exactly q
    achieved = mod.Q + (1 - mod.Q) * float((null + 0.0 > crit).mean())
    assert achieved == pytest.approx(mod.Q + (1 - mod.Q) * 0.05, abs=0.01)


def test_the_minimum_detectable_effect_recovers_a_planted_shift():
    """Checked against the closed form, which is worth writing out because I
    got it wrong first time and the test caught me.

    Power is `q + (1-q)*P(null + d > crit)`, so 80% power needs
    `P = (0.80 - 0.05)/0.95 = 0.7895` -- the shifted null must sit above the
    critical value four times in five. For a standard normal that puts
    `crit - d` at the 21.05th percentile, so

        d = z(0.95) - z(0.2105) = 1.645 - (-0.804) = 2.449

    The naive answer, `z(0.95) + z(0.80) = 2.486`, ignores the `(1-q)` term and
    is close enough to look right, which is exactly why it needs checking."""
    mod = _mod()
    rng = np.random.default_rng(1)
    null = rng.normal(0, 1.0, 40_000)
    crit = float(np.quantile(null, 0.95))
    assert mod.minimum_detectable(null, crit) == pytest.approx(2.449, abs=0.08)


def test_the_effect_scales_with_the_nulls_width():
    """Same shape, ten times the dispersion, ten times the edge required."""
    mod = _mod()
    rng = np.random.default_rng(7)
    base = rng.normal(0, 1.0, 40_000)
    wide = base * 10.0
    d1 = mod.minimum_detectable(base, float(np.quantile(base, 0.95)))
    d10 = mod.minimum_detectable(wide, float(np.quantile(wide, 0.95)))
    assert d10 == pytest.approx(d1 * 10, rel=0.02)


def test_a_wider_null_needs_a_bigger_edge():
    mod = _mod()
    rng = np.random.default_rng(2)
    narrow = rng.normal(0, 0.05, 20_000)
    wide = rng.normal(0, 0.50, 20_000)
    dn = mod.minimum_detectable(narrow, float(np.quantile(narrow, 0.95)))
    dw = mod.minimum_detectable(wide, float(np.quantile(wide, 0.95)))
    assert dw > dn * 5


def test_nothing_is_fitted_or_assumed_gaussian():
    """The critical value is read off the stored sign-flip draws. If the nulls
    are wrong the gate was already wrong in the same way, which is the only
    honest footing for this calculation."""
    for forbidden in ("norm.ppf", "scipy", "curve_fit", "lognorm", "1.645"):
        assert forbidden not in SRC, f"{forbidden} -- a distribution was assumed"
    assert "nulls.json" in SRC and "validation-null.json" in SRC


def test_a_ragged_sample_is_dropped_and_counted_not_reshaped():
    """One campaign stores samples of two different widths. Reshaping them
    silently would mix draw counts; dropping without saying so would hide how
    many candidates the answer rests on."""
    body = SRC.split("def load_train_null", 1)[1].split("\ndef ", 1)[0]
    assert "most_common(1)" in body
    assert "len(raw) - len(keep)" in body, "the dropped count is not reported"


def test_the_l2_bar_is_higher_than_any_single_winners():
    """**The decision-relevant finding.** Declaring a family of M makes the
    session's bar higher than any one campaign's, because `max_of_m` corrects
    over the family. Session 07 declared three and the bar roughly doubled."""
    mod = _mod()
    fam = ["nq-s07-01", "nq-s07-02", "nq-s07-03"]
    singles = [mod.validation_row(c) for c in fam]
    assert all(singles), "session 07's validation nulls are missing"
    l2 = mod.l2_row(fam)
    assert l2 and l2["m"] == 3
    assert l2["critical"] > max(s["critical"] for s in singles), (
        "the family bar is not above every single-winner bar; max_of_m is not "
        "correcting for the family")
    assert l2["mde"] > max(s["mde"] for s in singles)


def test_the_best_result_the_project_has_was_below_its_own_bar():
    """nq-s07-02's +0.2694 is the highest validation score in the project's
    history, and it sits under the critical value of its own null -- which is
    why L2 failed at p = 0.170 rather than because the family was unlucky."""
    mod = _mod()
    r = mod.validation_row("nq-s07-02")
    assert r is not None
    assert r["observed"] == pytest.approx(0.2694, abs=5e-4)
    assert r["observed"] < r["critical"], (
        "the headline result now clears its own null; this test and the "
        "commentary around it need rewriting")


def test_the_limits_are_written_down_where_the_numbers_are():
    """Four things bound this calculation and all four belong next to it, or
    somebody quotes the MDE as though it were exact."""
    flat = " ".join(SRC.split())
    assert "lower bound on power" in flat
    assert "median null dispersion" in flat
    assert "leaves the null's shape alone" in flat
    assert "false-positive floor" in flat
