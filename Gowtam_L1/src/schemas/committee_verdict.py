"""Committee's final verdict (PLAN_NEXT B.4/B.5): PASS/VETO binding decision."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CommitteeVerdict(BaseModel):
    candidate_id: str
    verdict: Literal["PASS", "VETO"]
    reasoning: str
    decision_status: Literal["Draft", "Provisional", "Locked", "Deprecated"] = "Locked"
