"""Resumability contract: per-candidate loop state (Task 0.5)."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class CandidateState(BaseModel):
    candidate_id: str
    status: Literal[
        "PLANNING",
        "BUILDING",
        "REVIEWING",
        "REPLANNING",
        "ENGINE_RUN",
        "PACKAGED",
        "FAILED",
    ]
    current_task_index: int = 0
    retry_count: int = 0
    replan_count: int = 0
    session_id: Optional[str] = None
    failure_reason: Optional[str] = None
