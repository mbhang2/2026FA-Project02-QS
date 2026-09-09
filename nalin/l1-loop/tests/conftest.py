"""Shared fixtures: isolate per-candidate on-disk state into tmp dirs.

``redirect_candidate_paths`` is opt-in (not autouse): unit tests that exercise
the driver/state/packaging paths request it, while the end-to-end smoke test
deliberately uses real project paths so OpenCode can discover its agent
definitions.
"""

from __future__ import annotations

import pytest

from src.loop import paths


@pytest.fixture
def redirect_candidate_paths(tmp_path, monkeypatch):
    def _dir(candidate_id):
        d = tmp_path / "candidates" / candidate_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(paths, "candidate_dir", _dir)
    return tmp_path
