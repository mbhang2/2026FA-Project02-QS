"""Source priors: the three things Gowtam's version got wrong, pinned.

His `Gowtam_hypothesis/researcher/bwis.py` fixed `alpha + beta = 10` for every
cell, never read the variance it computed, and thresholded a point estimate.
Each of those is a test here, written so that reintroducing the defect fails.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.research import bwis  # noqa: E402


# ------------------------------------------------- 1. concentration is real
def test_concentration_follows_the_observed_count():
    """**The central defect in the original.** A cell resting on 284 papers
    and one resting on 3 must not report the same confidence."""
    thin = bwis.from_counts(1, 2)
    thick = bwis.from_counts(100, 200)
    # Not equal means: the Jeffreys prior pulls a 3-observation cell toward
    # 0.5 much harder than a 300-observation one, which is correct and is why
    # the tolerance is loose rather than why the test is wrong.
    assert thin.mean == pytest.approx(thick.mean, abs=0.05)
    assert thick.concentration > 50 * thin.concentration
    assert thick.sd < thin.sd / 5, (
        "a cell with a hundred times the evidence must be much tighter")


def test_an_unobserved_cell_is_uninformative_not_average():
    """Jeffreys, so "nothing seen" reads as "no idea" rather than "0.5, firmly"."""
    p = bwis.from_counts(0, 0)
    assert p.concentration == pytest.approx(1.0)
    lo, hi = p.interval(0.80)
    assert hi - lo > 0.5, f"an empty cell reported a {hi - lo:.2f}-wide interval"


# ------------------------------------------------------ 2. variance is used
def test_pooling_weights_by_precision_not_by_count_of_sources():
    """His `hypothesis_score` averaged the means, so one vague source could
    drag a well-evidenced one. Inverse-variance weighting is the fix."""
    tight = bwis.from_counts(200, 200)          # mean 0.5, very tight
    vague = bwis.from_counts(1, 0)              # mean ~0.75, very wide
    pooled = bwis.pool([tight, vague])
    naive = (tight.mean + vague.mean) / 2
    assert abs(pooled.mean - tight.mean) < abs(naive - tight.mean) / 5, (
        f"pooled {pooled.mean:.4f} sits near the naive mean {naive:.4f}; "
        f"the vague source is being given equal weight")


def test_pooling_thin_sources_does_not_manufacture_confidence():
    thin = [bwis.from_counts(1, 1) for _ in range(2)]
    pooled = bwis.pool(thin)
    assert pooled.concentration == pytest.approx(
        sum(p.concentration for p in thin))
    assert pooled.sd > 0.15, "two thin cells pooled into a confident one"


def test_pooling_nothing_is_uninformative_rather_than_an_error():
    assert bwis.pool([]).concentration == pytest.approx(1.0)


# ------------------------------------------------ 3. the band knows it is unsure
def test_the_same_mean_on_different_evidence_can_give_different_bands():
    """The real strengthening, and the test I first wrote wrong.

    I expected a 2-of-3 cell to read `mid`. It reads `high`, because its exact
    80% interval is [0.315, 0.898] and that genuinely excludes a 0.259
    baseline -- weak evidence is still evidence, and at 80% mass that is the
    correct Bayesian statement. **Raising `BAND_MASS` until the assertion
    passed would have been tuning a threshold to match an intuition**, which
    is the one move this project does not make.

    The property that actually distinguishes this from a point-estimate
    threshold is below: two cells with the SAME mean and different evidence
    get different bands, which no threshold on the mean can express.
    """
    base = 0.259
    thin = bwis.from_counts(1, 1)                  # mean 0.50, n = 2
    thick = bwis.from_counts(77, 76)               # mean 0.50, n = 153
    assert thin.mean == pytest.approx(thick.mean, abs=0.01)
    assert bwis.band(thin, baseline=base) == "mid", (
        "a 50% rate on two observations was called a source that beats the "
        "corpus; the interval must still straddle the baseline")
    assert bwis.band(thick, baseline=base) == "high"


def test_the_interval_is_exact_and_not_normal_approximated():
    """`Beta(2.5, 1.5)` normal-approximated has an 80% lower bound of 0.472;
    exactly it is 0.315. The approximation was most confident where the
    evidence was thinnest, which is the failure the band exists to prevent."""
    # Thin cell: the approximation overstates the lower bound and puts the
    # upper bound outside the support. Both are errors the exact form avoids.
    p = bwis.from_counts(1, 0)
    lo, hi = p.interval(0.80)
    normal_lo = p.mean - 1.282 * p.sd
    normal_hi = p.mean + 1.282 * p.sd
    assert lo < normal_lo - 0.05, (
        f"exact lower bound {lo:.3f} is not below the normal approximation's "
        f"{normal_lo:.3f}; the approximation is back")
    assert normal_hi > 1.0 >= hi, (
        f"the normal upper bound {normal_hi:.3f} should leave the support "
        f"while the exact one ({hi:.3f}) cannot")

    # Thick cell: they agree, which is why this only matters for thin ones.
    thick = bwis.from_counts(77, 76)
    tlo, thi = thick.interval(0.80)
    assert tlo == pytest.approx(thick.mean - 1.282 * thick.sd, abs=0.002)
    # and the CDF is a real CDF
    assert bwis.beta_cdf(1.0, 2.5, 1.5) == pytest.approx(1.0, abs=1e-9)
    med = bwis._beta_quantile(0.5, 2.5, 1.5)
    assert bwis.beta_cdf(med, 2.5, 1.5) == pytest.approx(0.5, abs=1e-6)


def test_a_cell_straddling_the_baseline_is_mid_even_with_many_observations():
    base = 0.259
    straddle = bwis.from_counts(25, 70)                # crossref's real shape
    lo, hi = straddle.interval()
    assert lo < base < hi
    assert bwis.band(straddle, baseline=base) == "mid"


def test_low_requires_the_interval_entirely_below_the_baseline():
    assert bwis.band(bwis.from_counts(36, 248), baseline=0.259) == "low"


# ------------------------------------------------------------- calibration
def test_the_calibration_counts_only_screened_papers():
    """An unscreened paper is missing data. Counting it as a miss would mark
    every source down for the corpus outrunning the screening budget."""
    papers = [{"source": "a", "feasibility": "direct"},
              {"source": "a", "feasibility": "infeasible"},
              {"source": "a"}]                     # not screened
    cal = bwis.calibrate(papers)
    assert cal.n_papers == 2
    assert cal.by_source["a"].n_observed == 2


def test_usable_is_direct_or_adaptable_and_nothing_else():
    assert bwis.USABLE == {"direct", "adaptable"}
    papers = [{"source": "s", "feasibility": f}
              for f in ("direct", "adaptable", "infeasible")]
    cal = bwis.calibrate(papers)
    assert cal.by_source["s"].alpha - bwis.PRIOR_HITS == pytest.approx(2.0)


def test_an_unseen_source_falls_back_to_the_global_prior():
    cal = bwis.calibrate([{"source": "a", "feasibility": "direct"}])
    assert cal.prior_for("never-seen") is cal.global_prior


def test_the_committed_calibration_matches_the_registry():
    """The CI form of `bwis_calibrate.py --check`: a registry that gained
    papers and a priors file that did not are what drifts apart silently."""
    cfg = bwis.CONFIG_PATH
    if not cfg.is_file():
        pytest.skip("config/bwis-priors.json not committed yet")
    registry = REPO_ROOT / "data" / "papers" / "registry.json"
    papers = list(json.loads(registry.read_text())["papers"].values())
    fresh = bwis.calibrate(papers)
    stored = json.loads(cfg.read_text())
    stored.pop("digest", None)
    assert stored == fresh.as_dict(), (
        "config/bwis-priors.json is stale; run scripts/bwis_calibrate.py")


def test_the_real_calibration_separates_the_sources_it_should():
    """Not a tuned expectation -- the registry's own numbers. openalex is half
    usable on 153 papers and arxiv an eighth on 284, which is the signal the
    whole idea rests on; if it vanishes, the priors are measuring noise."""
    cfg = bwis.CONFIG_PATH
    if not cfg.is_file():
        pytest.skip("not calibrated yet")
    cal = bwis.read(cfg)
    assert cal.n_papers >= 500
    assert bwis.band(cal.prior_for("openalex"), baseline=cal.baseline) == "high"
    assert bwis.band(cal.prior_for("arxiv"), baseline=cal.baseline) == "low"


def test_the_priors_file_round_trips():
    cfg = bwis.CONFIG_PATH
    if not cfg.is_file():
        pytest.skip("not calibrated yet")
    cal = bwis.read(cfg)
    assert cal.digest
    assert 0.0 < cal.baseline < 1.0


def test_an_old_schema_is_refused_rather_than_misread(tmp_path):
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"schema": "bwis-priors/1", "global_floor": {}}))
    with pytest.raises(ValueError, match="bwis-priors/2"):
        bwis.read(p)
