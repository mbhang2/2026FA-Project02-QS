"""The driver: wire retrieval + generation + firewall + handoff into one run.

`run(ask)` is the researcher's single entry point. It emits a *population* of
linted hypotheses into `config/hypotheses/` and records provenance to a JSONL,
deciding nothing about which is "best" -- that is the campaign gate's job, not
the researcher's.

The hand-off order is load-bearing and must not be reordered by an eager edit:

1. render the record to markdown,
2. run **our** leak-lint (realized-outcome prose),
3. run **the rail's** gate-vocabulary lint (`src.core.loop.hypothesis.lint`),
4. only then write to `config/hypotheses/`.

The rail lint is the final authority; there is no code path that writes without
passing it. `config/hypotheses/` is in the rail's `ARTIFACT_GLOBS`, so a file
that lands there is a pre-registration whose commit date is its pre-registration
date.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import generate, leak_lint, llm
from .feasibility import DatasetCaps, Infeasible, feasible
from .render import render
from .retrieval import retrieve
from .schemas import HypothesisRecord

REPO_ROOT = Path(__file__).resolve().parents[2]  # Gowtam_hypothesis -> repo root
DEFAULT_OUTPUT_DIR = REPO_ROOT / "config" / "hypotheses"
DEFAULT_LOG_DIR = Path(__file__).resolve().parents[1] / "output"

# Bounds, at module top so they are visibly not buried in logic (L1 style).
MAX_FAMILIES = 6
PER_BACKEND = 6


@dataclass
class RunResult:
    ask: str
    emitted: list[HypothesisRecord] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    log_path: Path | None = None
    started_at: float = field(default_factory=time.time)

    @property
    def n_emitted(self) -> int:
        return len(self.emitted)

    @property
    def n_blocked(self) -> int:
        return len(self.blocked)


class RailUnavailable(RuntimeError):
    """The MVP's rail lint cannot be imported; nothing is written without it."""


class LintRejected(RuntimeError):
    """A rendered hypothesis failed a deterministic lint (ours or the rail's)."""


def _rail_lint(text: str, source: str) -> None:
    """Import the MVP's gate-vocabulary lint and run it. Nothing is written without it."""
    # `src` is the MVP's top-level package, so the repo root (not src/) goes on
    # the path for `from src.core.loop.hypothesis import ...` to resolve.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from src.core.loop.hypothesis import (  # noqa: PLC0415
            HypothesisRejected,
            lint as rail_lint_fn,
        )
    except ImportError as exc:
        raise RailUnavailable(
            f"cannot import src.core.loop.hypothesis from {REPO_ROOT}: {exc}") from None
    try:
        rail_lint_fn(text, source=source)
    except HypothesisRejected as exc:
        raise LintRejected(str(exc)) from None


def _write(rec: HypothesisRecord, output_dir: Path) -> Path:
    text = render(rec)
    try:
        leak_lint.lint(text, source=f"researcher:{rec.id}")
    except leak_lint.OutcomeLeak as exc:
        raise LintRejected(str(exc)) from None
    _rail_lint(text, source=f"researcher:{rec.id}")  # gate vocabulary (final)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{rec.id}.md"
    path.write_text(text)
    return path


def run(ask: str, *, budget: int = 3, caps: DatasetCaps = DatasetCaps(),
        output_dir: Path | None = None, log_dir: Path | None = None,
        per_backend: int = PER_BACKEND) -> RunResult:
    """Emit up to `budget` hypotheses for `ask`. Never decides which is best."""
    output_dir = output_dir or DEFAULT_OUTPUT_DIR
    log_dir = log_dir or DEFAULT_LOG_DIR
    result = RunResult(ask=ask)

    llm.preflight(generate.PROPOSE_MODEL)  # refuse to run on a substituted model

    families = generate.decompose(ask)[:MAX_FAMILIES]
    for family in families[:budget]:
        event = {"family": family, "at": time.time()}
        try:
            clips = retrieve(family, per_backend=per_backend)
            rec = generate.generate_hypothesis(
                family, clips, finalize=lambda r: _lint_findings(r, clips))
            if not feasible(rec, caps):
                raise Infeasible("; ".join(_caps_violations(rec, caps)))
            path = _write(rec, output_dir)
            result.emitted.append(rec)
            event.update({"outcome": "emitted", "id": rec.id, "path": str(path)})
        except (generate.PassBlocked, Infeasible, LintRejected,
                RailUnavailable, llm.LLMError, llm.ModelSubstituted) as exc:
            event.update({"outcome": "blocked",
                          "reason": f"{type(exc).__name__}: {exc}"})
            result.blocked.append(event)
        _append_log(log_dir, result.ask, event)

    result.log_path = _log_path(log_dir)
    return result


def _lint_findings(rec: HypothesisRecord, clips) -> list[str]:
    """Deterministic lint errors for a record, as refine-able feedback strings.

    Fed into `generate.generate_hypothesis(finalize=...)` so a gate-vocabulary or
    realized-outcome leak is refined out inside the loop rather than hard-blocked.
    Also flags a *hallucinated* citation: a source URL that was not among the
    retrieved clips is an invented reference, and the refine fixes it (or blocks).
    """
    text = render(rec)
    errors: list[str] = []
    try:
        leak_lint.lint(text, source=f"researcher:{rec.id}")
    except leak_lint.OutcomeLeak as exc:
        errors.append(str(exc))
    try:
        _rail_lint(text, source=f"researcher:{rec.id}")
    except LintRejected as exc:
        errors.append(str(exc))

    clip_urls = {_norm_url(c.url) for c in clips}
    for sc in rec.sources:
        if _norm_url(sc.source_url) not in clip_urls:
            errors.append(
                f"bad_source: cited {sc.source_url!r}, which was not among the "
                "retrieved sources -- cite only the sources you were given")
    # `RailUnavailable` (import failure) propagates -- a broken rail is a harness
    # failure, not something to refine prose around.
    return errors


def _norm_url(url: str) -> str:
    import re
    return re.sub(r"https?://", "", url).rstrip("/").lower()


def _caps_violations(rec: HypothesisRecord, caps: DatasetCaps) -> list[str]:
    from .feasibility import violations
    return violations(rec, caps)


def _append_log(log_dir: Path | None, ask: str, event: dict) -> None:
    if log_dir is None:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(_log_path(log_dir), "a") as f:
        f.write(json.dumps({"ask": ask, **event}) + "\n")


def _log_path(log_dir: Path) -> Path:
    return log_dir / "research_log.jsonl"
