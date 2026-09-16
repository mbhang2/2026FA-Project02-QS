"""Evidence packet types and constants for the audit agent MVP.

Implementation lives in engine/; this module documents the contract
agents and downstream tools should rely on.
"""

from __future__ import annotations

from typing import Any, Literal

GateStatus = Literal["PASS", "FAIL", "WEAK", "SKIP"]
DeskAction = Literal["kill", "modify", "pause"]
OverallVerdict = Literal["PASS", "FAIL"]

# Gate evaluation order for critics (see GATES.md).
GATE_ORDER: tuple[str, ...] = (
    "reproduce",
    "leak",
    "pit",
    "execution",
    "freeze",
    "stats",
)

HARD_GATES: frozenset[str] = frozenset(
    {"reproduce", "leak", "pit", "execution", "freeze"}
)


def experiment_row(packet: dict[str, Any]) -> dict[str, Any] | None:
    """First experiment row from an audit_experiment_id packet."""
    rows = packet.get("experiments") or []
    return rows[0] if rows else None


def passed_audit(packet: dict[str, Any]) -> bool:
    row = experiment_row(packet)
    if row is None:
        return packet.get("verdict") == "PASS"
    return row.get("verdict") == "PASS"


def gate_summary(row: dict[str, Any]) -> dict[str, str]:
    gates = row.get("gates") or {}
    return {name: str(gates.get(name, "—")) for name in GATE_ORDER}
