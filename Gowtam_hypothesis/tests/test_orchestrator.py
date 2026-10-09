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


def test_orchestrate_surfaces_http_failure(monkeypatch):
    from researcher import llm
    exc = llm.LLMError(
        "HTTP 403", status=403, model="claude-haiku-4-5",
        endpoint="https://llm-proxy.invalid/v1/chat/completions",
        provider="team LiteLLM gateway",
        content_type="text/html", body="<html>ModSecurity</html>")
    monkeypatch.setattr(orchestrator.llm, "call", lambda *a, **k: (_ for _ in ()).throw(exc))
    with pytest.raises(Exception) as e:
        orchestrator.orchestrate([orchestrator.Trajectory(outcome="dry_run")])
    msg = str(e.value)
    assert "HTTP 403" in msg
    assert "model=claude-haiku-4-5" in msg
    assert "llm-proxy.invalid" in msg
    assert "team LiteLLM gateway" in msg
    assert "ModSecurity" in msg
    assert "returned no JSON" not in msg


def test_orchestrate_reports_unparseable_reply(monkeypatch):
    from researcher import llm
    monkeypatch.setattr(
        orchestrator.llm, "call",
        lambda *a, **k: llm.Completion(text="no json here at all",
                                      model_served="claude-haiku-4-5"))
    with pytest.raises(Exception) as e:
        orchestrator.orchestrate([orchestrator.Trajectory(outcome="dry_run")])
    assert "no parseable JSON" in str(e.value)
    assert "no json here at all" in str(e.value)
