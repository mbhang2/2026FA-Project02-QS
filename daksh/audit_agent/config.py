"""Paths and aliases for the audit agent MVP."""

from __future__ import annotations

from pathlib import Path

# Repo root: daksh/audit_agent/config.py → parents[2]
REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_LEDGER = REPO_ROOT / "research" / "audit_db.json"

# Slash-command aliases (see commands/audit.md)
ALIASES: dict[str, str] = {
    "kill": "01-lookahead-cheat",
    "pause": "01-realistic",
}
