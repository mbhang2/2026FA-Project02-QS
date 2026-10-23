"""Bayesian Weighted Information System (BWIS) engine.

Assigns prior weights to retrieved sources using Beta distributions calibrated
from the Luna evaluation run (906 sources, 100 research questions), then updates
those priors with evidence from the critique pass to produce a per-hypothesis
``bwis_score``.

Design constraints
------------------
- No third-party dependencies: pure ``math`` and ``statistics``.
- Priors are loaded once at module import from the config file; the path is
  env-overridable so tests can inject a fixture without touching the filesystem.
- The prior table is static until a human manually runs ``promote_priors.py``.
  This prevents on-the-fly prior drift from contaminating the calibration.
- ``bwis_score`` is *logged* and *advisory*; it never gates hypothesis emission.
"""

from __future__ import annotations

import json
import math
import os
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .retrieval import SourceClip

# ---------------------------------------------------------------------------
# Config path: override with BWIS_CONFIG env var (tests use this)
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "bwis_probe" / "bwis_config.json"

def _load_config() -> dict:
    path = Path(os.environ.get("BWIS_CONFIG", str(_DEFAULT_CONFIG)))
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


_CONFIG: dict = _load_config()

# ---------------------------------------------------------------------------
# SourcePrior — a Beta distribution summary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourcePrior:
    """Beta(alpha, beta) distribution over source quality in [0, 1]."""
    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        a, b = self.alpha, self.beta
        total = a + b
        return (a * b) / (total * total * (total + 1))

    def __repr__(self) -> str:
        return f"SourcePrior(mean={self.mean:.4f}, n={self.alpha+self.beta:.1f})"


def _from_cfg(node: dict) -> SourcePrior:
    return SourcePrior(alpha=float(node["alpha"]), beta=float(node["beta"]))


# Module-level constants derived from the config (with safe fallbacks)
GLOBAL_FLOOR: SourcePrior = (
    _from_cfg(_CONFIG["global_floor"])
    if "global_floor" in _CONFIG
    else SourcePrior(alpha=5.234, beta=4.766)
)

_BY_CLASS:   dict[str, SourcePrior] = {k: _from_cfg(v) for k, v in _CONFIG.get("by_class", {}).items()}
_BY_THEME:   dict[str, SourcePrior] = {k: _from_cfg(v) for k, v in _CONFIG.get("by_theme", {}).items()}
_BY_BACKEND: dict[str, SourcePrior] = {k: _from_cfg(v) for k, v in _CONFIG.get("by_backend", {}).items()}

# ---------------------------------------------------------------------------
# Prior lookup
# ---------------------------------------------------------------------------

def _infer_class(clip: "SourceClip") -> str:
    """Best-effort source class from URL and backend."""
    url = clip.url.lower()
    backend = getattr(clip, "backend", "unknown").lower()
    if backend == "arxiv" or "arxiv.org" in url:
        return "academic"
    if "semantic" in backend or "scholar.org" in url or "ssrn.com" in url:
        return "academic"
    if any(d in url for d in ("bis.org", "federalreserve.gov", "ecb.europa.eu",
                               "imf.org", "worldbank.org", "oecd.org", "sifma.org")):
        return "institutional"
    if any(d in url for d in ("medium.com", "substack.com", "quantopian.com",
                               "quantstrategylab", "alphaarchitect.com",
                               "breakingdownfinance.com", "reasonabledeviations.com")):
        return "practitioner"
    return "other web"


def prior_for(clip: "SourceClip", theme: str = "") -> SourcePrior:
    """Return a starting prior for one source clip.

    Lookup order (most specific → least):
    1. Class × theme blend (0.6 × class + 0.4 × theme) when both are known.
    2. Class prior alone.
    3. Backend prior alone.
    4. Global floor.
    """
    src_class = _infer_class(clip)
    class_prior = _BY_CLASS.get(src_class, GLOBAL_FLOOR)
    theme_prior = _BY_THEME.get(theme, None)

    if theme_prior is not None:
        # Blend: weighted sum of means, then reconstruct a Beta with the same
        # effective N as the class prior (conservative: don't inflate confidence).
        blended_mean = 0.6 * class_prior.mean + 0.4 * theme_prior.mean
        n = class_prior.alpha + class_prior.beta
        return SourcePrior(alpha=blended_mean * n, beta=(1.0 - blended_mean) * n)

    backend = getattr(clip, "backend", "unknown").lower()
    if src_class == "other web" and backend in _BY_BACKEND:
        # For generic web sources the backend is the best signal we have.
        return _BY_BACKEND[backend]

    return class_prior


# ---------------------------------------------------------------------------
# Evidence table — pseudo-observation counts and success probability
# ---------------------------------------------------------------------------

# Each entry is (n_pseudo_obs, p_success).
# Values are deliberately conservative to avoid per-question over-fitting;
# accumulation across many questions is where signal builds.
EVIDENCE: dict[str, tuple[float, float]] = {
    "pass":          (3.0, 0.90),   # critic passed; source clearly supported the claim
    "weak_support":  (1.0, 0.60),   # source cited but critic was lukewarm / flagged minor issues
    "not_cited":     (0.0, 0.50),   # source retrieved but never cited (no information update)
    "contradicted":  (2.0, 0.15),   # source cited but critique flagged it as off-topic or contradictory
}


def update(prior: SourcePrior, verdict_label: str) -> SourcePrior:
    """Bayesian update of a Beta prior with one evidence observation.

    Args:
        prior: the current Beta distribution for this source.
        verdict_label: one of the keys in EVIDENCE.

    Returns:
        Updated SourcePrior.  If the label is unknown, returns prior unchanged.
    """
    entry = EVIDENCE.get(verdict_label)
    if entry is None:
        return prior
    n_obs, p = entry
    return SourcePrior(
        alpha=prior.alpha + n_obs * p,
        beta=prior.beta  + n_obs * (1.0 - p),
    )


# ---------------------------------------------------------------------------
# Hypothesis score
# ---------------------------------------------------------------------------

def hypothesis_score(cited_posteriors: list[SourcePrior]) -> float:
    """Posterior-weighted mean over cited sources.

    A hypothesis with no cited sources returns the global floor (0.523).
    A hypothesis with one excellent source is not penalised for retrieval breadth.
    """
    if not cited_posteriors:
        return GLOBAL_FLOOR.mean
    return statistics.mean(p.mean for p in cited_posteriors)


def bwis_band(score: float) -> str:
    """Directional band for orchestrator consumption.

    The orchestrator sees the band string only, never the raw float, to prevent
    it from optimising against the calibration directly.

    Thresholds are derived from the Luna run distribution:
        - high  ≥ 0.65  (cited-source quality floor from the run)
        - low   < 0.50  (below global retrieval average)
        - mid   in between
    """
    if score >= 0.65:
        return "high"
    if score < 0.50:
        return "low"
    return "mid"


# ---------------------------------------------------------------------------
# Convenience: score a full record
# ---------------------------------------------------------------------------

def score_record(
    clips: list["SourceClip"],
    cited_urls: set[str],
    critic_passed: bool,
    theme: str = "",
) -> tuple[float, str]:
    """Compute bwis_score and bwis_band for one hypothesis.

    Args:
        clips: all retrieved clips for the question.
        cited_urls: the set of URLs that the hypothesis cited (from SourceClaim).
        critic_passed: whether the CriticVerdict verdict was "pass".
        theme: the research theme string (used for prior lookup).

    Returns:
        (bwis_score, bwis_band_str)
    """
    priors = {c.url: prior_for(c, theme) for c in clips}
    posteriors_cited: list[SourcePrior] = []

    for clip in clips:
        prior = priors[clip.url]
        if clip.url in cited_urls:
            label = "pass" if critic_passed else "weak_support"
            posteriors_cited.append(update(prior, label))
        else:
            # not_cited: prior unchanged, excluded from the hypothesis score
            update(prior, "not_cited")  # no-op by design; kept for clarity

    score = hypothesis_score(posteriors_cited)
    return score, bwis_band(score)


# ---------------------------------------------------------------------------
# Empirical Backtest Feedback
# ---------------------------------------------------------------------------

BACKTEST_EVIDENCE: dict[str, tuple[float, float]] = {
    "accept":       (5.0, 0.95),   # passed pre-registered sign-flip MC gate and positive score
    "positive":     (2.0, 0.65),   # observed_best > 0, but did not clear multiple-testing hurdle
    "flat_or_loss": (2.0, 0.25),   # observed_best <= 0 or rejected
}


def record_backtest_feedback(
    hypothesis_id: str,
    theme: str,
    sources: list[dict],
    observed_best: float | None,
    gate_accept: bool,
    feedback_file: Path | None = None,
) -> Path:
    """Record empirical backtest performance alongside cited literature sources."""
    import datetime as _dt
    path = feedback_file or (Path(__file__).resolve().parents[1] / "output" / "bwis_feedback.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)

    verdict = "accept" if gate_accept else (
        "positive" if (observed_best is not None and observed_best > 0) else "flat_or_loss"
    )

    record = {
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "hypothesis_id": hypothesis_id,
        "theme": theme,
        "observed_best": observed_best,
        "gate_accept": gate_accept,
        "empirical_verdict": verdict,
        "sources": sources,
    }
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return path

