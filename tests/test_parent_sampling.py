"""Parent sampling: which file the proposer is asked to edit.

The mix is in the sampler, not in the prompt. Default epsilon 0 is champion-only,
which is every campaign before the field. Parents come from this campaign, never
from last campaign's winner. `show_history=False` disables the draw, because an
old source in `{current}` would leak the control arm.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.core.loop import llm  # noqa: E402

BASELINE = (REPO_ROOT / "src" / "strategies" / "current.py").read_text()


def _agent(**kw):
    import os

    os.environ.setdefault("LITELLM_BASE_URL", "https://example.invalid/v1")
    os.environ.setdefault("LITELLM_API_KEY", "not-a-key-and-never-sent")
    return llm.LLMAgent(model="test-model", **kw)


def _current_slot(prompt: str) -> str:
    _, rest = prompt.split("### The current strategy", 1)
    return rest.split("### Your prior trials", 1)[0]


def _history(tmp_path: Path, text: str, name: str = "old.py") -> list[dict]:
    path = tmp_path / name
    path.write_text(text)
    return [{"outcome": "completed", "strategy_source": str(path),
             "score": {"primary": 0.1, "trade_count": 3, "n_sessions": 10,
                       "rejected": False}}]


def test_default_epsilon_is_champion_only_so_old_campaigns_do_not_move(tmp_path):
    champ = tmp_path / "champion.py"
    champ.write_text("class Strategy:\n    pass\n# champion\n")
    history = _history(tmp_path, "class Strategy:\n    pass\n# other\n")
    parent = _agent(champion=champ)._pick_parent(history)
    assert parent.mode == "exploit" and parent.kind == "champion"
    assert "# champion" in parent.text and "# other" not in parent.text


def test_epsilon_zero_puts_the_champion_in_the_current_slot(tmp_path):
    champ = tmp_path / "champion.py"
    champ.write_text("class Strategy:\n    pass\n# champion\n")
    history = _history(tmp_path, "class Strategy:\n    pass\n# other\n")
    agent = _agent(champion=champ, explore_epsilon=0.0)
    slot = _current_slot(agent._prompt(history, repair=""))
    assert "# champion" in slot
    assert "# other" not in slot


def test_epsilon_one_edits_the_only_non_champion_source(tmp_path):
    """Champion equals the committed baseline, so the pool is one prior trial."""
    champ = tmp_path / "champion.py"
    champ.write_text(BASELINE)
    history = _history(tmp_path, "class Strategy:\n    pass\n# other\n")
    parent = _agent(champion=champ, explore_epsilon=1.0)._pick_parent(history)
    assert parent.mode == "explore"
    assert parent.kind == "trial" and parent.trial == 0
    assert "# other" in parent.text


def test_the_same_seed_and_history_redraw_the_same_parent_after_a_resume(tmp_path):
    champ = tmp_path / "champion.py"
    champ.write_text("class Strategy:\n    pass\n# champion\n")
    history = _history(tmp_path, "class Strategy:\n    pass\n# other\n")
    a = _agent(champion=champ, explore_epsilon=0.5, parent_rng_seed=7)
    b = _agent(champion=champ, explore_epsilon=0.5, parent_rng_seed=7)
    assert a._pick_parent(history) == b._pick_parent(history)


def test_repair_does_not_resample_the_parent(tmp_path):
    champ = tmp_path / "champion.py"
    champ.write_text("class Strategy:\n    pass\n# champion\n")
    history = _history(tmp_path, "class Strategy:\n    pass\n# other\n")
    agent = _agent(champion=champ, explore_epsilon=1.0)
    agent._parent = agent._pick_parent(history)
    first = agent._parent
    agent._prompt(history, repair="fix the syntax")
    assert agent._parent is first


def test_history_withheld_never_leaks_a_past_source_as_current(tmp_path):
    champ = tmp_path / "champion.py"
    champ.write_text("class Strategy:\n    pass\n# champion\n")
    history = _history(tmp_path, "class Strategy:\n    pass\n# other\n")
    parent = _agent(champion=champ, explore_epsilon=1.0,
                    show_history=False)._pick_parent(history)
    assert parent.mode == "exploit"
    assert "# other" not in parent.text


def test_an_empty_explore_pool_falls_back_to_the_champion(tmp_path):
    """Champion, baseline, and every prior source are the same file."""
    champ = tmp_path / "champion.py"
    champ.write_text(BASELINE)
    history = _history(tmp_path, BASELINE)
    parent = _agent(champion=champ, explore_epsilon=1.0)._pick_parent(history)
    assert parent.mode == "exploit" and parent.kind == "champion"


def test_a_bad_epsilon_is_refused_at_construction():
    with pytest.raises(ValueError, match="explore_epsilon"):
        _agent(explore_epsilon=1.5)
    with pytest.raises(ValueError, match="explore_epsilon"):
        _agent(explore_epsilon=-0.1)


def test_drain_log_records_the_parent_without_the_source(tmp_path):
    champ = tmp_path / "champion.py"
    champ.write_text("class Strategy:\n    pass\n# champion\n")
    history = _history(tmp_path, "class Strategy:\n    pass\n# other\n")
    agent = _agent(champion=champ, explore_epsilon=0.0)
    agent._parent = agent._pick_parent(history)
    log = agent.drain_log()
    assert log["parent"] == {"mode": "exploit", "kind": "champion",
                             "trial": None, "digest": agent._parent.digest}
    assert "text" not in log["parent"]


def test_config_pins_the_draw_not_a_prompt_ratio():
    cfg = _agent(explore_epsilon=0.2, parent_rng_seed=3).config()
    assert cfg["explore_epsilon"] == 0.2
    assert cfg["parent_rng_seed"] == 3
