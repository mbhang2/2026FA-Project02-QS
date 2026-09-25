"""Amendment 6: exposed in too few scored sessions -> rejected, never scored."""
from __future__ import annotations
import sys
from pathlib import Path
import pytest
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec   # noqa: E402
from src.core.engine import Engine                                    # noqa: E402
from src.core.scoring.scorer import score, score_returns              # noqa: E402
from src.core.scoring import metrics as M                             # noqa: E402
from src.core.types import Split                                      # noqa: E402

SPEC = SyntheticSpec(n_sessions=200, ticks_per_session=390, vol_per_tick=0.15, seed=1,
                     train_frac=0.6, validation_frac=0.2)

class TradesFirstK:
    """Enters once in each of the first K scored sessions, then never again."""
    def __init__(self, k): self.k = k; self.done = set()
    def setup(self, ctx): ctx.subscribe("5m")
    def on_bars(self, ctx):
        if not ctx.actionable: return
        h = ctx.history("5m")
        if not h or ctx.position or ctx.working_order_ids: return
        s = h[-1].session
        if len(self.done) < self.k and s not in self.done:
            self.done.add(s); ctx.buy(size=1, stop_offset=0.5, target_offset=0.5)
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass

def run(k):
    e = Engine.for_test(SyntheticDataset(SPEC), TradesFirstK(k), split=Split.TRAIN,
                        warmup_sessions=0, fill_model="unfavorable", seed=1)
    e.run(); return e.reconstructed(), e.session_calendar()

def test_the_function_default_is_no_floor():
    """The conditional null, the calibration rig and the power sweep all call
    score() with defaults. A floor there would reject inside a null."""
    b, cal = run(3)
    s = score(b, period=cal)
    assert "Amendment 6" not in (s.reject_reason or "")

def test_below_the_floor_is_rejected_and_the_reason_names_both_numbers():
    b, cal = run(5)
    n = len(cal); exposed = M.sessions_exposed(b, cal)
    assert exposed < 0.70 * n, (exposed, n)
    s = score(b, period=cal, min_exposed_fraction=0.70)
    assert s.rejected
    assert f"exposed in {exposed} of {n}" in s.reject_reason
    assert "70%" in s.reject_reason and "Amendment 6" in s.reject_reason

def test_at_or_above_the_floor_is_scored_normally():
    b, cal = run(10_000)                       # trades every scored session
    n = len(cal); exposed = M.sessions_exposed(b, cal)
    assert exposed >= 0.70 * n
    s_floor = score(b, period=cal, min_exposed_fraction=0.70)
    s_none = score(b, period=cal)
    assert not s_floor.rejected or s_floor.reject_reason == s_none.reject_reason
    assert s_floor.primary == s_none.primary

def test_the_null_never_sees_the_floor():
    """round_turn resamples via score_returns, which has no such parameter."""
    import inspect
    assert "min_exposed_fraction" not in inspect.signature(score_returns).parameters
    src = (REPO_ROOT / "src/core/scoring/randomization/round_turn.py").read_text()
    assert "min_exposed_fraction" not in src

def test_the_scored_path_passes_it_to_both_scores():
    src = (REPO_ROOT / "run.py").read_text()
    assert src.count("min_exposed_fraction=min_exposed") == 2, (
        "the floor must reach the charged AND the uncharged score, or a rejected "
        "candidate would still carry an uncharged figure")

def test_config_default_and_record_field():
    import yaml
    cfg = yaml.safe_load((REPO_ROOT / "config/run.yaml").read_text())
    assert cfg["campaign"]["min_exposed_fraction"] == 0.70
    from src.core.loop.campaign import CampaignRecord
    r = CampaignRecord(campaign_id="x", budget=1, profile="harness", q=0.05)
    assert r.min_exposed_fraction is None            # digest-safe for old records


# --------------------------------------------------------------------------- #
# Amendment 6 says "the ledger row carries the count and the floor". Until
# 2026-09-25 it did so only inside `reject_reason`, so nq-ml-03's fifteen SCORED
# rows recorded nothing and a reader could not tell a candidate that cleared the
# floor by thirty sessions from one that cleared it by one.
# --------------------------------------------------------------------------- #
def test_a_row_that_clears_the_floor_still_records_how_close_it_came():
    b, cal = run(10_000)                       # trades every scored session
    exposed = M.sessions_exposed(b, cal)
    s = score(b, period=cal, min_exposed_fraction=0.70)
    assert s.rejected is False
    assert s.sessions_exposed == exposed, "the count must survive on a passing row"
    assert s.min_exposed_fraction == 0.70


def test_a_rejected_row_carries_the_count_as_a_field_not_only_as_prose():
    b, cal = run(5)
    exposed = M.sessions_exposed(b, cal)
    s = score(b, period=cal, min_exposed_fraction=0.70)
    assert s.rejected is True
    assert s.sessions_exposed == exposed and s.min_exposed_fraction == 0.70
    assert f"exposed in {exposed} of" in s.reject_reason, "the prose half stays"


def test_no_floor_means_not_measured_and_is_distinct_from_zero():
    """`score()` is called without a floor by the conditional null, the
    calibration rig and the power sweep. Those rows must not claim an exposure
    of 0, which is a real count."""
    b, cal = run(5)
    s = score(b, period=cal)
    assert s.sessions_exposed == -1, "-1 means the floor was never applied"
    assert s.min_exposed_fraction == 0.0
