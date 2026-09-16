"""Planner's output contract: an atomic task plan (Task 0.3)."""

from __future__ import annotations

from pydantic import BaseModel


class Task(BaseModel):
    task_id: str
    description: str
    target_files: list[str]
    acceptance: str


class TaskPlan(BaseModel):
    candidate_id: str
    tasks: list[Task]
