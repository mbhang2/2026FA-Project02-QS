#!/usr/bin/env python3
"""Validate the knowledge store. Exit non-zero on the first class of failure.

    python scripts/knowledge_check.py

The log is append-only prose about prior campaigns. This check is what stops
a score, a parameter pair, or a date window from sitting in a field the
compiler would emit. It also asserts the proposer cannot see `log.jsonl`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.loop import knowledge as K  # noqa: E402


def _llm_does_not_read_the_log() -> list[str]:
    src = (ROOT / "src" / "core" / "loop" / "llm.py").read_text()
    errors = []
    if "log.jsonl" in src or "docs/knowledge" in src:
        errors.append(
            "src/core/loop/llm.py names the knowledge store. The proposer "
            "reads a campaign-local snapshot passed in as a KnowledgeView; "
            "it must not open docs/knowledge/ itself."
        )
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", type=Path, default=K.LOG_PATH)
    args = ap.parse_args()
    errors = K.check_store(args.log)
    errors.extend(K.check_campaign_index(K.load_log(args.log)))
    errors.extend(_llm_does_not_read_the_log())
    try:
        view = K.compile_view(dataset="nq-trades")
    except K.KnowledgeRejected as exc:
        errors.append(str(exc))
        view = None
    if errors:
        print("knowledge_check: FAIL", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    n_closed = view.count("#### Do not re-explore") if view else 0
    print(f"knowledge_check: ok  ({len(K.load_log(args.log))} rows, "
          f"nq-trades snapshot {len(view or '')} bytes, "
          f"closed-section={bool(n_closed)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
