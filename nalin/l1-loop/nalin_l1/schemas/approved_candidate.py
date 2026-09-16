"""Input contract: an approved research candidate (Task 0.2).

``ApprovedCandidate`` is frozen at the end of Phase 0. It is currently produced
by a hand-written stub (``fixtures/sample_candidate.json``) because nothing
upstream exists yet; the shape is what the future debate/judge gate will emit.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ApprovedCandidate(BaseModel):
    candidate_id: str
    hypothesis: str
    source_paper_ref: Optional[str] = None
    asset_ids: list[str]
    start: str
    train_end: str
    val_end: str
    end: str
    prior_context: list[str] = []
