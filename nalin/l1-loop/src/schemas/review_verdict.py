"""Reviewer's output contract (Task 0.4)."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, model_validator


class ReviewVerdict(BaseModel):
    task_id: str
    verdict: Literal["PROCEED", "MINOR_FIX", "REPLAN"]
    reasoning: str
    fix_instructions: Optional[str] = None
    replan_reasoning: Optional[str] = None

    @model_validator(mode="after")
    def _check_required_fields(self) -> "ReviewVerdict":
        if self.verdict == "MINOR_FIX" and not self.fix_instructions:
            raise ValueError(
                f"MINOR_FIX verdict for task {self.task_id!r} requires fix_instructions"
            )
        if self.verdict == "REPLAN" and not self.replan_reasoning:
            raise ValueError(
                f"REPLAN verdict for task {self.task_id!r} requires replan_reasoning"
            )
        return self
