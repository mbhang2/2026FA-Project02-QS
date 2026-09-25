import pytest

from researcher import orchestrator
from researcher.orchestrator import Trajectory, TrajectoryLeak


def test_to_trajectory_whitelists_known_fields():
    t = orchestrator.to_trajectory(
        {"outcome": "scored", "trades": 3, "sessions": 100,
         "score_dir": "up", "turnover_dir": "down"})
    assert t.score_dir == "up"
    assert t.trades == 3


def test_to_trajectory_rejects_unknown_fields():
    with pytest.raises(TrajectoryLeak):
        orchestrator.to_trajectory({"outcome": "scored", "p_value": 0.01})


def test_to_trajectory_rejects_gate_verdict_field():
    with pytest.raises(TrajectoryLeak):
        orchestrator.to_trajectory({"outcome": "scored", "verdict": "ACCEPT"})


def test_trajectory_is_frozen_whitelist():
    # `n` is a gate concept; it must not be in the allowed set.
    assert "n" not in TrajectoryLeak.ALLOWED
    assert "verdict" not in TrajectoryLeak.ALLOWED
