"""Pre-flight: does a hypothesis's data need fit the subscription the engine has?

The campaign loop costs a trial (which counts toward N). A hypothesis that asks
for tick data the harness serves only as bars, or a bar interval that does not
exist in the dataset, is a wasted look -- and the *researcher* can know this
before handing a hypothesis off. This module is the mechanical version of that
knowledge: explicit capabilities, checked before ``config/hypotheses/`` is
written.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schemas import (
    DataRequires,
    HypothesisRecord,
    MAX_GRID_SIZE,
    MAX_PARAMS,
)


@dataclass(frozen=True, slots=True)
class DatasetCaps:
    """What the harness can actually serve for the instrument under research.

    The NQ/ES/QQQ adapter serves 5m bars; ticks/spreads are not part of the
    researcher's subscription surface. Keep this explicit and committed rather
    than inferred, because a wrong capability here silently lets the researcher
    hand over hypotheses the campaign has no data for.
    """

    intervals: frozenset[str] = frozenset({"5m"})
    has_ticks: bool = False
    has_spreads: bool = False
    max_params: int = MAX_PARAMS
    max_grid_size: int = MAX_GRID_SIZE


class Infeasible(ValueError):
    """A hypothesis needs data the harness cannot serve, named by what."""


def feasible(rec: HypothesisRecord, caps: DatasetCaps) -> bool:
    """True only when every ``data_requires`` field is satisfiable."""
    return not violations(rec, caps)


def violations(rec: HypothesisRecord, caps: DatasetCaps) -> list[str]:
    dr: DataRequires = rec.data_requires
    out: list[str] = []
    if dr.interval not in caps.intervals:
        out.append(f"interval {dr.interval!r} not in {sorted(caps.intervals)}")
    if dr.needs_ticks and not caps.has_ticks:
        out.append("needs ticks, harness serves bars only")
    if dr.needs_spreads and not caps.has_spreads:
        out.append("needs spreads, harness does not serve them")
    if hasattr(rec, "specification") and rec.specification:
        spec = rec.specification
        if len(spec.parameters) > caps.max_params:
            out.append(
                f"parameters count {len(spec.parameters)} exceeds limit of {caps.max_params}")
        grid = spec.get_total_grid_size()
        if grid > caps.max_grid_size:
            out.append(
                f"grid size {grid:,} exceeds search budget limit of {caps.max_grid_size:,}")
    return out


def check(rec: HypothesisRecord, caps: DatasetCaps, *, source: str = "") -> None:
    """Raise `Infeasible` naming every violation, or return."""
    v = violations(rec, caps)
    if v:
        raise Infeasible(
            f"{source or rec.id} needs data the harness cannot serve: "
            + "; ".join(v))
