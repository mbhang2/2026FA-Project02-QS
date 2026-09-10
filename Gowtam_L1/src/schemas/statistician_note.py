"""Statistician's output (PLAN_NEXT B.4): methodology notes on raw results."""

from __future__ import annotations

from pydantic import BaseModel


class StatisticianNote(BaseModel):
    candidate_id: str
    summary: str
    concerns: list[str] = []
