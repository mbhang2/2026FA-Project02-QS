#!/usr/bin/env python3
"""Is amendment N in force? Read from the document, never from a flag.

    python scripts/prereg.py            # list every amendment and its status

`docs/design/pre-registration.md` is the protocol, and an amendment's status line
is how a human signs one off. Two scripts need to ask -- `holdout.py` about
Amendment 9, `l2_gate.py` about Amendment 10 -- and both asked by parsing the file
themselves, which is one copy away from the two disagreeing about whether a rule
is in force.

**The document is the switch.** A boolean in code, or a key in `run.yaml`, would
let the protocol and the behaviour drift apart silently: somebody flips the flag
and the amendment still says DRAFTED, or signs the amendment and the code keeps
its old behaviour. Reading the prose means the signature and the behaviour cannot
be separated, and the git history of that one file is the record of when each rule
came into force.

## The parse, and the substring bug it exists to kill

`holdout.py` asked "is 'IN FORCE' present and 'NOT IN FORCE' absent?". That reads
**"REGISTERED, NOT YET IN FORCE"** as in force, because "NOT IN FORCE" is not a
substring of it -- and Amendments 4 and 7 are both written that way. The rule
guarding the single irrecoverable look would have unlocked on a status line that
says, in English, the opposite.

So the rule here is positional rather than a substring search:

- **No `**Status:` line** -- the amendment is an unconditional dated statement and
  is in force from its date. Amendments 1 and 2 are these.
- **A `**Status:` line** -- in force **iff the status begins with "IN FORCE"**.
  That accepts "IN FORCE from 2026-09-30, signed off by Michael" and rejects
  "REGISTERED, NOT YET IN FORCE", "DRAFTED, NOT IN FORCE" and "REGISTERED, NOT
  YET IMPLEMENTED", which are the four forms this document actually uses.

A status line is written by a human for a human. Requiring it to *start* with the
signature is the one rule that cannot be satisfied by a sentence explaining why
something is not signed yet.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
PREREG = ROOT / "docs" / "design" / "pre-registration.md"
HEAD = 600


def _head(n: int, text: str | None = None) -> str | None:
    """The opening of amendment `n`'s section, or None if there is no such one."""
    text = PREREG.read_text() if text is None else text
    # The em dash is required: "## Amendment 6's disclosure gap" is a section
    # ABOUT amendment 6, not amendment 6, and must not be mistaken for its head.
    m = re.search(rf"^## Amendment {n}\s+[\u2014-]", text, flags=re.MULTILINE)
    return None if m is None else text[m.start():m.start() + HEAD]


def _status_line(head: str) -> str | None:
    """The text between `**Status:` and the closing `**`, and nothing after it.

    The status is bold-delimited and the prose continues on the same line, so a
    naive strip of trailing asterisks returns the first sentence of the body too.
    """
    for line in head.splitlines():
        if line.startswith("**Status:"):
            body = line[len("**Status:"):]
            end = body.find("**")
            return (body if end < 0 else body[:end]).strip()
    return None


def in_force(n: int, text: str | None = None) -> bool:
    """Whether amendment `n` has been signed off.

    A missing section answers False: an amendment that cannot be read is not one
    anybody signed, and every caller here is gating something irreversible.
    """
    head = _head(n, text)
    if head is None:
        return False
    line = _status_line(head)
    if line is None:
        # No status line at all: an unconditional dated amendment (1 and 2).
        return True
    return line.upper().startswith("IN FORCE")


def status(n: int, text: str | None = None) -> str:
    """A readable one-liner for a refusal message or a log."""
    head = _head(n, text)
    if head is None:
        return f"Amendment {n}: no such section"
    line = _status_line(head)
    if line is not None:
        return f"Amendment {n}: {line}"
    return f"Amendment {n}: unconditional, in force from its date (no status line)"


def all_amendments() -> list[tuple[int, bool, str]]:
    text = PREREG.read_text()
    ns = sorted({int(m.group(1)) for m in
                 re.finditer(r"^## Amendment (\d+)\b", text, flags=re.MULTILINE)})
    return [(n, in_force(n, text), status(n, text)) for n in ns]


def main() -> int:
    for n, ok, line in all_amendments():
        print(f"  {'IN FORCE' if ok else '--------'}  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
