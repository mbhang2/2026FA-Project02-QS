"""CLI entrypoint: `python -m researcher "<ask>" [--budget N] [--loop]`.

Run from the `Gowtam_hypothesis/` directory. The gateway key is read from the
environment (`LITELLM_API_KEY`; load `.env` first with `set -a && source .env`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import driver, orchestrator


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m researcher",
        description="Research an ask into a population of linted hypotheses.",
    )
    p.add_argument("ask", nargs="+", help="the research ask (quote multi-word)")
    p.add_argument("--budget", type=int, default=3,
                   help="max hypotheses to emit (default 3)")
    p.add_argument("--output-dir", type=str, default=None,
                   help="write hypotheses here (default: config/hypotheses/)")
    p.add_argument("--loop", action="store_true",
                   help="run the orchestration loop, not a single driver run")
    p.add_argument("--live", action="store_true",
                   help="with --loop: actually launch campaigns "
                        "(default is dry-run)")
    return p


def _main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    ask = " ".join(args.ask)

    if args.loop:
        from .driver import run as _emit

        def emit(ask: str):
            return _emit(ask, budget=args.budget).emitted

        result = orchestrator.run_loop(
            ask, emit_fn=emit, dry_run=not args.live)
        print(f"orchestration loop complete; iterations={result.iterations}")
        for d in result.decisions:
            print(f"  next -> {d.next_ask!r}  ({d.why!r})")
        return 0

    out = Path(args.output_dir) if args.output_dir else None
    res = driver.run(ask, budget=args.budget, output_dir=out)
    print(f"ask: {ask!r}")
    print(f"emitted: {res.n_emitted}  blocked: {res.n_blocked}")
    for rec in res.emitted:
        print(f"  EMIT  {rec.id:<40} {rec.falsifiable[:72]}")
    for b in res.blocked:
        print(f"  BLOCK {b.get('family', '')[:40]:<40} {b.get('reason', '')[:60]}")
    if res.log_path:
        print(f"provenance: {res.log_path}")
    print("files written to:", out or driver.DEFAULT_OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
