#!/usr/bin/env python3
"""Compile the agent snapshot from the knowledge store.

    python scripts/knowledge_compile.py --dataset nq-trades
    python scripts/knowledge_compile.py --dataset nq-trades --output /tmp/knowledge.md
    python scripts/knowledge_compile.py --dataset nq-trades --check --output FILE

`--check` rebuilds and compares to `--output`. `nq_campaign.py --stage register`
calls `src.core.loop.knowledge.write_view` directly and writes the bytes into
the campaign directory, so two campaigns cannot share a mutable file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.loop import knowledge as K  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="nq-trades")
    ap.add_argument("--output", type=Path)
    ap.add_argument("--check", action="store_true",
                    help="rebuild and require --output to match")
    args = ap.parse_args()
    text = K.compile_view(dataset=args.dataset)
    if args.check:
        if args.output is None:
            sys.exit("--check needs --output")
        got = args.output.read_text() if args.output.is_file() else ""
        if got != text:
            sys.exit(f"{args.output} is stale; run without --check to rewrite it")
        print(f"knowledge_compile: {args.output} matches")
        return 0
    if args.output is not None:
        args.output.write_text(text)
        print(f"wrote {args.output}  ({len(text)} bytes, digest "
              f"{K.digest_text(text)[:12]})")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
