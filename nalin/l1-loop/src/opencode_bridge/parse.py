"""Schema-validated parse-and-retry wrapper (Task 2.2)."""

from __future__ import annotations

from typing import Any, Callable, Type

from pydantic import BaseModel

from src.opencode_bridge.invoke import extract_result_block


class ParseRetryError(Exception):
    """Raised when parsing fails after ``max_retries`` retries."""


def parse_or_retry(
    raw_output: str,
    schema: Type[BaseModel],
    retry_fn: Callable[[Exception], str],
    max_retries: int = 2,
) -> BaseModel:
    """Parse ``raw_output`` into ``schema``, retrying via ``retry_fn`` on failure.

    ``retry_fn`` is called with the parse/validation error and must return a fresh
    transcript (it is responsible for re-invoking the agent with the error
    appended to the original prompt). After ``max_retries`` unsuccessful retries,
    raises :class:`ParseRetryError` (a hard failure).
    """
    attempts = 0
    while True:
        try:
            block = extract_result_block(raw_output)
            return schema.model_validate_json(block)
        except Exception as exc:
            if attempts >= max_retries:
                raise ParseRetryError(
                    f"failed to parse result into {schema.__name__} "
                    f"after {max_retries} retries: {exc}"
                ) from exc
            attempts += 1
            raw_output = retry_fn(exc)
