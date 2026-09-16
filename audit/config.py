"""Paths and aliases for the audit agent MVP."""

from __future__ import annotations

from pathlib import Path

# Repo root: audit/config.py -> parents[1]. Was parents[2] when this package
# lived at daksh/audit_agent/; Task 44 moved it one level up and the walk moved
# with it.
REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_LEDGER = REPO_ROOT / "research" / "audit_db.json"

# Slash-command aliases (see commands/audit.md)
ALIASES: dict[str, str] = {
    "kill": "01-lookahead-cheat",
    "pause": "01-realistic",
}
