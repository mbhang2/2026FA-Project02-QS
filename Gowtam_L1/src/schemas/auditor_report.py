"""Auditor's output (PLAN_NEXT B.4): four-axis bias/leakage/overfit report."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AuditFinding(BaseModel):
    axis: Literal["lookahead", "bias", "overfitting", "assumptions"]
    verdict: Literal["pass", "flag", "veto"]
    detail: str


class AuditorReport(BaseModel):
    candidate_id: str
    findings: list[AuditFinding]

    @property
    def leak_veto(self) -> bool:
        return any(
            f.axis == "lookahead" and f.verdict == "veto" for f in self.findings
        )
