#!/usr/bin/env python3
"""Is every row in the ledger a row anyone could check?

    python scripts/ledger_check.py                    # cheap checks, every row
    python scripts/ledger_check.py --replay           # re-run each row too
    python scripts/ledger_check.py --json

The ledger is the p-value column. False-discovery control over it assumes each
row is a calibrated number from a reproducible run, and a row nobody can
reproduce is not a p-value -- it is a number with a timestamp. Carrying one
silently is the same loophole in honest N that the ledger exists to close, in the
direction of overcounting rather than under, and wrong either way.

Three questions per row, cheapest first:

1. **Is the code it names still in history?** `git_sha` has to be a commit
   reachable from HEAD. Since Task 22 a row also carries `code_fingerprint`, a
   hash of the engine, data and scoring sources on disk, and that is recomputed
   from the tree at `git_sha` and compared. A row whose sha exists but whose
   fingerprint does not match was produced with uncommitted edits -- which is
   normal for an agent's strategy and is *not* normal for the harness.
2. **Does it say what produced it?** `artifacts` -- the pre-run digests of the
   null tables, calendars and profiles -- plus a stored strategy source.
3. **Does it reproduce?** `--replay` only, because it re-walks the data. The row
   reproduces if the replayed blotter digest equals the recorded one, and where
   the row carries a fill log the P&L is *recomputed* from the fills rather than
   re-observed.

**Superseded rows are marked, never deleted.** Deleting a row loses the fact that
a trial happened, which is the one thing the ledger cannot afford to lose; a row
left unmarked corrupts the column. So supersessions live in their own append-only
file, `runs/supersessions.jsonl`, one entry per superseded row naming it by index
and blotter digest, with a reason. The ledger itself is never rewritten.

This is meant to be a gate run before a campaign, not after it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
# `scripts/` is deliberately not a package -- it is build-time, and `test_layout`
# asserts nothing under `src.core` can reach it. So sibling scripts are imported
# the way a script imports a sibling, by putting the directory on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.core.scoring import integrity  # noqa: E402

SUPERSESSIONS = REPO_ROOT / "runs" / "supersessions.jsonl"


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_supersessions(path: Path = SUPERSESSIONS) -> dict[int, dict]:
    """`{row index: entry}`. Keyed by index *and* checked against the digest, so
    an entry cannot silently start pointing at a different row when the ledger
    grows a line above it."""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            entry = json.loads(line)
            out[entry["row"]] = entry
    return out


def sha_is_in_history(sha: str) -> bool:
    if not sha:
        return False
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
        cwd=REPO_ROOT, capture_output=True,
    ).returncode == 0


def code_fingerprint_at(sha: str) -> str | None:
    """Recompute `code_fingerprint` from the tree at `sha`.

    `git archive` into a scratch directory rather than checking anything out:
    this runs against a working tree someone is using, and a check that moves
    HEAD to answer a question is a check that can lose work.
    """
    archive = subprocess.run(["git", "archive", sha], cwd=REPO_ROOT,
                             capture_output=True)
    if archive.returncode != 0:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        extract = subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout,
                                 capture_output=True)
        if extract.returncode != 0:
            return None
        return integrity.code_fingerprint(Path(tmp))


def check_row(index: int, row: dict, *, replay: bool,
              superseded: dict[int, dict]) -> dict:
    sha = row.get("git_sha", "")
    result = {
        "row": index,
        "purpose": row.get("purpose"),
        "profile": row.get("profile"),
        "outcome": row.get("outcome"),
        "git_sha": sha[:12],
        "sha_in_history": sha_is_in_history(sha),
        "has_code_fingerprint": bool(row.get("code_fingerprint")),
        "has_artifacts": bool(row.get("artifacts")),
        "has_fill_log": bool(row.get("fill_log_source")),
        "strategy_source_stored": bool(row.get("strategy_source")),
        "code_fingerprint_matches": None,
        "reproduces": None,
        "notes": [],
    }

    if row.get("code_fingerprint") and result["sha_in_history"]:
        at_sha = code_fingerprint_at(sha)
        result["code_fingerprint_matches"] = at_sha == row["code_fingerprint"]
        if at_sha is not None and not result["code_fingerprint_matches"]:
            result["notes"].append(
                "harness source differs from the commit the row names "
                "(uncommitted edits at run time)"
            )
    elif not row.get("code_fingerprint"):
        result["notes"].append("row predates code_fingerprint (added in Task 22)")
    if not result["has_artifacts"]:
        result["notes"].append("row predates the artifact digest set (Task 22)")

    if index in superseded:
        entry = superseded[index]
        result["superseded"] = entry.get("reason", "")
        if entry.get("blotter_digest") not in (None, row.get("blotter_digest")):
            result["notes"].append(
                "SUPERSESSION POINTS AT A DIFFERENT ROW: the ledger has shifted "
                "under it and the entry must be re-checked by hand"
            )

    if replay and row.get("blotter_digest"):
        result["reproduces"] = _replay_reproduces(row, result)
    return result


def _replay_reproduces(row: dict, result: dict) -> bool | None:
    from replay import replay as do_replay

    try:
        engine = do_replay(row)
    except Exception as exc:                                # noqa: BLE001
        result["notes"].append(f"replay refused: {type(exc).__name__}: {exc}")
        return None
    if engine.blotter.digest() != row["blotter_digest"]:
        result["notes"].append(
            f"replayed {engine.blotter.digest()[:12]}, "
            f"recorded {row['blotter_digest'][:12]}"
        )
        return False
    recorded_log = row.get("fill_log_digest")
    if recorded_log and engine.fill_log_digest() != recorded_log:
        result["notes"].append("blotter matches but the fill log does not")
        return False
    return True


def verdict(result: dict) -> str:
    if "superseded" in result:
        return "superseded"
    if result["reproduces"] is False:
        return "UNREPRODUCIBLE"
    if not result["sha_in_history"]:
        return "UNVERIFIABLE"
    if result["code_fingerprint_matches"] is False:
        return "unpinned"
    if result["reproduces"] is True:
        return "reproduces"
    return "checkable"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ledger", nargs="?", type=Path,
                    default=REPO_ROOT / "runs" / "ledger.jsonl")
    ap.add_argument("--replay", action="store_true",
                    help="re-run every row and compare digests. Slow, and the "
                         "only check that actually answers the question.")
    ap.add_argument("--supersessions", type=Path, default=SUPERSESSIONS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rows = load_rows(args.ledger)
    superseded = load_supersessions(args.supersessions)
    results = [check_row(i, r, replay=args.replay, superseded=superseded)
               for i, r in enumerate(rows)]

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print(f"{args.ledger}  {len(rows)} rows\n")
        print(f"{'row':>3}  {'purpose':<8} {'profile':<14} {'sha':<12} "
              f"{'verdict':<15} notes")
        for r in results:
            note = "; ".join(r["notes"]) if r["notes"] else ""
            if "superseded" in r:
                note = f"superseded: {r['superseded']}" + (f"; {note}" if note else "")
            print(f"{r['row']:>3}  {r['purpose'] or '-':<8} {r['profile'] or '-':<14} "
                  f"{r['git_sha']:<12} {verdict(r):<15} {note}")

        counted = [r for r in results
                   if r["purpose"] == "trial" and "superseded" not in r]
        print()
        print(f"N (purpose=trial, not superseded): {len(counted)}")
        print(f"superseded: {sum(1 for r in results if 'superseded' in r)}")
        if args.replay:
            print(f"reproduces: {sum(1 for r in results if r['reproduces'] is True)}"
                  f" / {sum(1 for r in results if r['reproduces'] is not None)} attempted")
        else:
            print("reproduction not attempted; re-run with --replay")

    bad = [r for r in results if verdict(r) in ("UNREPRODUCIBLE", "UNVERIFIABLE")]
    if bad:
        print(f"\n{len(bad)} row(s) cannot be verified. Supersede them with a "
              f"reason in {args.supersessions.name} or fix what broke; do not "
              f"delete them.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
