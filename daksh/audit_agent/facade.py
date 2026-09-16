"""Public API for the audit agent MVP — frozen engine + ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from daksh.audit_agent.config import ALIASES, DEFAULT_LEDGER, REPO_ROOT
from daksh.audit_agent.engine import (
    append_records,
    audit_experiment_id,
    latest_by_experiment,
    list_experiments as _list,
    load_db,
    presentation_row,
    records_from_packet,
)


def resolve_id(experiment_id: str) -> str:
    """Map command aliases to concrete experiment ids."""
    key = experiment_id.strip()
    return ALIASES.get(key, key)


def list_experiments(*, root: Path | None = None) -> list[dict[str, str]]:
    return _list(root or REPO_ROOT)


def run_audit(
    experiment_id: str,
    *,
    seed: int = 7,
    root: Path | None = None,
    ledger: Path | None = None,
    store: bool = True,
) -> dict[str, Any]:
    """Run frozen auditor once; optionally append ledger row."""
    exp_id = resolve_id(experiment_id)
    packet = audit_experiment_id(exp_id, seed=seed, root=root or REPO_ROOT)
    if store:
        ticket = None
        lookup = packet.get("lookup") or {}
        if lookup.get("ticket"):
            ticket = Path(lookup["ticket"]).name
        records = records_from_packet(packet, seed=seed, ticket=ticket)
        append_records(records, path=ledger or DEFAULT_LEDGER)
    return packet


def read_ledger(
    experiment_id: str | None = None,
    *,
    ledger: Path | None = None,
) -> list[dict[str, Any]]:
    """Return presentation rows; latest row if experiment_id set."""
    db = load_db(ledger or DEFAULT_LEDGER)
    if experiment_id:
        exp_id = resolve_id(experiment_id)
        row = latest_by_experiment(db, exp_id)
        return [presentation_row(row)] if row else []
    return [presentation_row(r) for r in db.get("records") or []]


def audit_experiment(
    experiment_id: str,
    *,
    seed: int = 7,
    root: Path | None = None,
) -> dict[str, Any]:
    """Audit without writing ledger (read-only probe)."""
    return audit_experiment_id(resolve_id(experiment_id), seed=seed, root=root or REPO_ROOT)


def format_brief(packet: dict[str, Any]) -> str:
    """One-screen summary for slides and CLI --brief."""
    lines = [
        f"VERDICT: {packet.get('verdict')}  action: {packet.get('action')}",
    ]
    for row in packet.get("experiments") or []:
        exp_id = row.get("experiment_id")
        kind = row.get("kind")
        leak = (row.get("leak") or {}).get("verdict")
        st = row.get("stats") or {}
        passed = row.get("verdict") == "PASS"
        sharpe = st.get("sharpe_annual", (row.get("metrics") or {}).get("sharpe"))
        psr = st.get("psr")
        lines.append(
            f"- {exp_id} ({kind}): passed_audit={passed} action={row.get('action')} "
            f"leak={leak} pit={row.get('pit_ok')} sharpe={sharpe} psr={psr}"
        )
        if row.get("next_step"):
            lines.append(f"    next: {row.get('next_step')}")
        for finding in row.get("findings") or []:
            lines.append(f"    {finding}")
    return "\n".join(lines)


def format_json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)
