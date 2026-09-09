"""Output contract: the packaged candidate (Task 0.6).

Frozen at the end of Phase 0. This is what the (not-yet-built) debate gate and
committee consume.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from src.schemas.approved_candidate import ApprovedCandidate
from src.schemas.review_verdict import ReviewVerdict
from src.schemas.task_plan import TaskPlan


class CandidatePackage(BaseModel):
    candidate: ApprovedCandidate
    plan: TaskPlan
    review_log: list[ReviewVerdict]
    trial_log: Optional[dict] = None
    final_status: Literal["PASSED_TO_COMMITTEE", "FAILED"]
    failure_reason: Optional[str] = None
