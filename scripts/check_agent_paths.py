#!/usr/bin/env python3
"""CI guard: the research agent may only ever change `src/strategies/**`.

Enforced two ways — tool permissions in the agent config, and this check. This
one is the one that actually holds, because configs drift and get copied wrong.

Run over a diff:

    python scripts/check_agent_paths.py --base origin/main
    python scripts/check_agent_paths.py --paths src/core/engine/engine.py  # for tests

Exits non-zero and names the offending paths. Human commits are not agent
commits: CI runs this only on branches the loop pushes, identified by the
`agent/` branch prefix, and `--force-check` runs it anywhere.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# The only tree the agent writes. Everything else is immutable to it.
AGENT_ALLOWLIST = ("src/strategies/",)

# Never writable, agent or not, without a deliberate human commit: if a strategy
# could reach the scorer it could optimize a number it controls.
SCORER_PATH = "src/core/scoring/scorer.py"


def violations(paths: list[str]) -> list[str]:
    """Paths outside the agent's allowlist, sorted. Pure, so it is testable
    without a git repository."""
    return sorted(
        p for p in paths if not any(p.startswith(a) for a in AGENT_ALLOWLIST)
    )


def changed_paths(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in out.stdout.splitlines() if p.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main", help="ref to diff against")
    ap.add_argument("--paths", nargs="*", help="explicit paths instead of a git diff")
    ap.add_argument(
        "--force-check",
        action="store_true",
        help="check even when the branch is not an agent branch",
    )
    args = ap.parse_args()

    if args.paths is not None:
        paths = list(args.paths)
    else:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not args.force_check and not branch.startswith("agent/"):
            print(f"branch {branch!r} is not an agent branch; skipping")
            return 0
        paths = changed_paths(args.base)

    bad = violations(paths)
    if bad:
        print("agent diff touches paths outside src/strategies/:", file=sys.stderr)
        for p in bad:
            note = "  <- the scorer" if p == SCORER_PATH else ""
            print(f"  {p}{note}", file=sys.stderr)
        return 1
    print(f"ok: {len(paths)} changed path(s), all under {AGENT_ALLOWLIST[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
