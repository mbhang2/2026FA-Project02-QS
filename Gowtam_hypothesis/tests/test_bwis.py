"""Unit tests for researcher/bwis.py — all offline, no LLM calls, no network."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXTURE_CONFIG = {
    "global_floor": {"alpha": 5.234, "beta": 4.766},
    "by_class": {
        "institutional": {"alpha": 6.833, "beta": 3.167},
        "academic":      {"alpha": 5.806, "beta": 4.194},
        "other web":     {"alpha": 4.932, "beta": 5.068},
        "practitioner":  {"alpha": 4.833, "beta": 5.167},
    },
    "by_theme": {
        "Execution and portfolio construction": {"alpha": 6.485, "beta": 3.515},
        "Structural and auction dynamics":      {"alpha": 3.646, "beta": 6.354},
    },
    "by_backend": {
        "arxiv":          {"alpha": 5.395, "beta": 4.605},
        "semantic_scholar": {"alpha": 4.735, "beta": 5.265},
        "searxng":        {"alpha": 5.492, "beta": 4.508},
        "unknown":        {"alpha": 5.000, "beta": 5.000},
    },
}


@pytest.fixture(autouse=True)
def inject_bwis_config(tmp_path, monkeypatch):
    """Write a fixture config and point BWIS_CONFIG at it for every test."""
    cfg = tmp_path / "bwis_config.json"
    cfg.write_text(json.dumps(_FIXTURE_CONFIG))
    monkeypatch.setenv("BWIS_CONFIG", str(cfg))

    # Reload the module so it picks up the env override.
    import importlib
    import researcher.bwis as bwis_mod
    importlib.reload(bwis_mod)
    # Re-import so all references in this test module point to the reloaded version.
    import researcher.bwis
    monkeypatch.setattr("researcher.bwis", researcher.bwis)
    return researcher.bwis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clip(url="https://example.com/paper", backend="arxiv", title="Test Paper"):
    """A minimal SourceClip stand-in (avoids importing the real dataclass)."""
    from researcher.retrieval import SourceClip
    return SourceClip(title=title, url=url, summary="test summary", backend=backend)


# ---------------------------------------------------------------------------
# SourcePrior
# ---------------------------------------------------------------------------

class TestSourcePrior:
    def test_mean(self, inject_bwis_config):
        bwis = inject_bwis_config
        p = bwis.SourcePrior(alpha=6.0, beta=4.0)
        assert abs(p.mean - 0.6) < 1e-9

    def test_variance_positive(self, inject_bwis_config):
        bwis = inject_bwis_config
        p = bwis.SourcePrior(alpha=5.0, beta=5.0)
        assert p.variance > 0


# ---------------------------------------------------------------------------
# Prior lookup ordering
# ---------------------------------------------------------------------------

class TestPriorLookup:
    def test_class_ordering_institutional_gt_academic(self, inject_bwis_config):
        bwis = inject_bwis_config
        inst  = _clip(url="https://bis.org/paper", backend="searxng")
        acad  = _clip(url="https://arxiv.org/abs/123", backend="arxiv")
        assert bwis.prior_for(inst).mean > bwis.prior_for(acad).mean

    def test_class_ordering_academic_gt_other_web(self, inject_bwis_config):
        bwis = inject_bwis_config
        acad  = _clip(url="https://arxiv.org/abs/999", backend="arxiv")
        other = _clip(url="https://medium.com/post", backend="searxng",
                      title="Medium post")
        assert bwis.prior_for(acad).mean > bwis.prior_for(other).mean

    def test_theme_blend_execution_gt_structural(self, inject_bwis_config):
        bwis = inject_bwis_config
        clip = _clip(url="https://arxiv.org/abs/1", backend="arxiv")
        p_exec   = bwis.prior_for(clip, theme="Execution and portfolio construction")
        p_struct = bwis.prior_for(clip, theme="Structural and auction dynamics")
        assert p_exec.mean > p_struct.mean

    def test_unknown_theme_falls_back_to_class(self, inject_bwis_config):
        bwis = inject_bwis_config
        clip = _clip(url="https://arxiv.org/abs/2", backend="arxiv")
        p_known   = bwis.prior_for(clip, theme="")
        p_unknown = bwis.prior_for(clip, theme="Nonexistent Theme XYZ")
        # Unknown theme → falls back to class-only prior → same mean
        assert abs(p_known.mean - p_unknown.mean) < 1e-9

    def test_global_floor_for_unknown_class_and_backend(self, inject_bwis_config):
        bwis = inject_bwis_config
        clip = _clip(url="https://some-random-blog.net/x", backend="unknown",
                     title="Random blog post")
        # Should not crash; must return something close to the global floor
        p = bwis.prior_for(clip)
        assert 0.3 < p.mean < 0.8  # sanity bounds

    def test_institutional_url_detected(self, inject_bwis_config):
        bwis = inject_bwis_config
        clip = _clip(url="https://www.imf.org/report/2024", backend="searxng")
        p = bwis.prior_for(clip)
        # institutional prior mean = 6.833 / (6.833 + 3.167) ≈ 0.683
        assert p.mean > 0.65


# ---------------------------------------------------------------------------
# Posterior update
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_pass_increases_mean(self, inject_bwis_config):
        bwis = inject_bwis_config
        prior = bwis.SourcePrior(alpha=5.0, beta=5.0)
        post  = bwis.update(prior, "pass")
        assert post.mean > prior.mean

    def test_contradicted_decreases_mean(self, inject_bwis_config):
        bwis = inject_bwis_config
        prior = bwis.SourcePrior(alpha=5.0, beta=5.0)
        post  = bwis.update(prior, "contradicted")
        assert post.mean < prior.mean

    def test_not_cited_is_noop(self, inject_bwis_config):
        bwis = inject_bwis_config
        prior = bwis.SourcePrior(alpha=5.0, beta=5.0)
        post  = bwis.update(prior, "not_cited")
        assert post.alpha == prior.alpha
        assert post.beta  == prior.beta

    def test_weak_support_between_pass_and_not_cited(self, inject_bwis_config):
        bwis = inject_bwis_config
        prior = bwis.SourcePrior(alpha=5.0, beta=5.0)
        post_pass = bwis.update(prior, "pass")
        post_weak = bwis.update(prior, "weak_support")
        assert prior.mean < post_weak.mean < post_pass.mean

    def test_unknown_label_returns_prior_unchanged(self, inject_bwis_config):
        bwis = inject_bwis_config
        prior = bwis.SourcePrior(alpha=5.0, beta=5.0)
        post  = bwis.update(prior, "nonexistent_label")
        assert post.alpha == prior.alpha


# ---------------------------------------------------------------------------
# Hypothesis score
# ---------------------------------------------------------------------------

class TestHypothesisScore:
    def test_no_sources_returns_global_floor(self, inject_bwis_config):
        bwis = inject_bwis_config
        score = bwis.hypothesis_score([])
        assert abs(score - bwis.GLOBAL_FLOOR.mean) < 1e-9

    def test_single_strong_source(self, inject_bwis_config):
        bwis = inject_bwis_config
        p = bwis.SourcePrior(alpha=8.0, beta=2.0)  # mean=0.8
        assert abs(bwis.hypothesis_score([p]) - 0.8) < 1e-9

    def test_mixed_sources_is_mean(self, inject_bwis_config):
        bwis = inject_bwis_config
        p1 = bwis.SourcePrior(alpha=8.0, beta=2.0)   # mean=0.80
        p2 = bwis.SourcePrior(alpha=6.0, beta=4.0)   # mean=0.60
        expected = (0.80 + 0.60) / 2
        assert abs(bwis.hypothesis_score([p1, p2]) - expected) < 1e-9

    def test_does_not_inflate_with_more_weak_sources(self, inject_bwis_config):
        """A hypothesis with 1 strong + 4 weak sources should score lower than 1 strong alone."""
        bwis = inject_bwis_config
        strong = bwis.SourcePrior(alpha=9.0, beta=1.0)   # mean=0.9
        weak   = bwis.SourcePrior(alpha=4.0, beta=6.0)   # mean=0.4
        score_one  = bwis.hypothesis_score([strong])
        score_many = bwis.hypothesis_score([strong, weak, weak, weak, weak])
        assert score_many < score_one


# ---------------------------------------------------------------------------
# bwis_band thresholds
# ---------------------------------------------------------------------------

class TestBwisBand:
    def test_high(self, inject_bwis_config):
        assert inject_bwis_config.bwis_band(0.70) == "high"

    def test_mid_upper(self, inject_bwis_config):
        assert inject_bwis_config.bwis_band(0.60) == "mid"

    def test_mid_lower(self, inject_bwis_config):
        assert inject_bwis_config.bwis_band(0.51) == "mid"

    def test_low(self, inject_bwis_config):
        assert inject_bwis_config.bwis_band(0.45) == "low"

    def test_boundary_high(self, inject_bwis_config):
        assert inject_bwis_config.bwis_band(0.65) == "high"

    def test_boundary_low(self, inject_bwis_config):
        # low is strictly < 0.50; exactly 0.50 is "mid"
        assert inject_bwis_config.bwis_band(0.50) == "mid"
        assert inject_bwis_config.bwis_band(0.499) == "low"


# ---------------------------------------------------------------------------
# score_record integration
# ---------------------------------------------------------------------------

class TestScoreRecord:
    def test_emitted_score_gt_floor(self, inject_bwis_config):
        bwis = inject_bwis_config
        clip = _clip(url="https://arxiv.org/abs/1", backend="arxiv")
        score, band = bwis.score_record([clip], {clip.url}, critic_passed=True)
        assert score > bwis.GLOBAL_FLOOR.mean

    def test_no_cited_returns_floor(self, inject_bwis_config):
        bwis = inject_bwis_config
        clip = _clip(url="https://arxiv.org/abs/2", backend="arxiv")
        score, band = bwis.score_record([clip], set(), critic_passed=True)
        assert abs(score - bwis.GLOBAL_FLOOR.mean) < 1e-9

    def test_critic_failed_scores_lower_than_passed(self, inject_bwis_config):
        bwis = inject_bwis_config
        clip = _clip(url="https://arxiv.org/abs/3", backend="arxiv")
        score_pass, _ = bwis.score_record([clip], {clip.url}, critic_passed=True)
        score_fail, _ = bwis.score_record([clip], {clip.url}, critic_passed=False)
        assert score_pass > score_fail


# ---------------------------------------------------------------------------
# promote_priors shift cap
# ---------------------------------------------------------------------------

class TestPromotePriors:
    def test_shift_cap_aborts_large_change(self, tmp_path, monkeypatch):
        """promote_priors must abort (SystemExit) when a shift > MAX_SHIFT."""
        from bwis_probe import promote_priors

        # A config with academic prior mean ≈ 0.58
        old_cfg = {
            "global_floor": {"alpha": 5.234, "beta": 4.766},
            "by_class": {"academic": {"alpha": 5.806, "beta": 4.194}},
            "by_theme": {},
            "by_backend": {},
        }
        # A new weight that shifts academic mean by 0.25 (>> MAX_SHIFT=0.10)
        new_weights = {
            "overall_bwis_score": 0.5234,
            "by_class": {"academic": {"avg_weight": 0.83, "count": 50}},
            "by_theme": {},
            "by_backend": {},
        }
        cfg_path = tmp_path / "bwis_config.json"
        inp_path = tmp_path / "new_weights.json"
        cfg_path.write_text(json.dumps(old_cfg))
        inp_path.write_text(json.dumps(new_weights))

        monkeypatch.setattr("sys.argv", [
            "promote_priors",
            "--input", str(inp_path),
            "--output", str(cfg_path),
            "--min-observations", "20",
        ])
        with pytest.raises(SystemExit) as exc_info:
            promote_priors.main()
        assert exc_info.value.code != 0

    def test_small_shift_accepted(self, tmp_path, monkeypatch):
        """promote_priors must succeed and update when shift <= MAX_SHIFT."""
        from bwis_probe import promote_priors

        old_cfg = {
            "global_floor": {"alpha": 5.234, "beta": 4.766},
            "by_class": {"academic": {"alpha": 5.806, "beta": 4.194}},
            "by_theme": {},
            "by_backend": {},
        }
        # shift = 0.62 - 0.58 = 0.04 < 0.10
        new_weights = {
            "overall_bwis_score": 0.5234,
            "by_class": {"academic": {"avg_weight": 0.62, "count": 50}},
            "by_theme": {},
            "by_backend": {},
        }
        cfg_path = tmp_path / "bwis_config.json"
        inp_path = tmp_path / "new_weights.json"
        cfg_path.write_text(json.dumps(old_cfg))
        inp_path.write_text(json.dumps(new_weights))

        monkeypatch.setattr("sys.argv", [
            "promote_priors",
            "--input", str(inp_path),
            "--output", str(cfg_path),
            "--min-observations", "20",
        ])
        rc = promote_priors.main()
        assert rc == 0
        result = json.loads(cfg_path.read_text())
        updated_mean = result["by_class"]["academic"]["alpha"] / (
            result["by_class"]["academic"]["alpha"] + result["by_class"]["academic"]["beta"]
        )
        assert abs(updated_mean - 0.62) < 0.01
