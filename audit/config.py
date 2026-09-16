"""Paths and aliases for the audit agent MVP."""

from __future__ import annotations

from pathlib import Path
from audit._roots import repo_root

# Anchored on a marker, never on a depth (see `audit/_roots.py`). This walked
# `parents[2]` from `daksh/audit_agent/config.py` and pointed one directory
# above the repository for the length of the Task 44 restructure.
REPO_ROOT = repo_root()

DEFAULT_LEDGER = REPO_ROOT / "research" / "audit_db.json"

# Slash-command aliases (see commands/audit.md)
ALIASES: dict[str, str] = {
    "kill": "01-lookahead-cheat",
    "pause": "01-realistic",
}
