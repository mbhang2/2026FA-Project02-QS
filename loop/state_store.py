"""Read/write ``state.json`` atomically (Task 4.4)."""

from __future__ import annotations

from loop import paths
from loop.schemas import CandidateState


def load_state(candidate_id: str) -> CandidateState | None:
    path = paths.candidate_file(candidate_id, "state.json")
    if not path.exists():
        return None
    return CandidateState.model_validate_json(path.read_text())


def save_state(state: CandidateState) -> None:
    """Write state atomically: temp file then rename, so a crash mid-write never
    leaves a corrupt ``state.json``."""
    path = paths.candidate_file(state.candidate_id, "state.json")
    tmp = path.with_name("state.json.tmp")
    tmp.write_text(state.model_dump_json(indent=2))
    tmp.replace(path)
