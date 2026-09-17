#!/usr/bin/env python3
"""Is every row in the ledger a row anyone could check?

    python scripts/ledger_check.py                    # EVERY ledger, every row
    python scripts/ledger_check.py --replay           # re-run each row too
    python scripts/ledger_check.py runs/ledger.jsonl  # just one
    python scripts/ledger_check.py --json

**With no argument this checks every ledger in the repository**, which means
`runs/ledger.jsonl` and every `runs/campaigns/**/ledger.jsonl`. It did not until
Task 47, and the gap was not theoretical: all 200 rows of the Task 30 null
campaign are unpinned -- run against a working tree with uncommitted edits, so
the code that produced them cannot be recovered from history -- and nobody knew,
because the standard invocation only ever looked at `runs/ledger.jsonl` and the
campaign trials do not live there. A check whose default scope excludes most of
the rows is a check that reports clean while the rows rot.

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
   re-observed. Each replay writes its own record to `runs/replays.jsonl` --
   every execution is logged, but a replay is not a candidate and does not belong
   in the file that is the denominator of N.

A row produced under a different environment is reported as **environment-
drifted**, which is not the same as superseded and is a weaker warning since
Task 28 than it was when it was written.

The original reason was Task 26's finding that the same trial scored differently
on macOS and Linux. That turned out not to be numpy: every input was
bit-identical and the divergence was `sum()`, which CPython 3.12 compensates and
3.11 does not. Money is summed with `math.fsum` now, and the same trial produces
a **bit-identical score on both platforms**, verified field by field.

So the flag no longer means "this row's number is not comparable". It means the
row was produced under a different set of pinned versions, which is worth knowing
for reasons that are no longer about arithmetic -- a changed adapter dependency
can change what the data *is*, and that the digests would catch, but only if
someone looks. Keep the flag, read it as provenance rather than as a numerical
caveat.

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


DEFAULT_LEDGER_KEY = "runs/ledger.jsonl"


def ledger_key(path: Path) -> str:
    """A ledger's name in the supersession file: repo-relative, POSIX."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def discover_ledgers(root: Path = REPO_ROOT) -> list[Path]:
    """Every ledger in the repository, the main one first.

    `runs/replays.jsonl` is deliberately absent: it records replays, which are
    not trials and have their own purpose. `docs/reviews/**` is absent too --
    those are a task's own scratch output, not the project's record.
    """
    found = []
    main = root / "runs" / "ledger.jsonl"
    if main.is_file():
        found.append(main)
    found.extend(sorted((root / "runs" / "campaigns").rglob("ledger.jsonl")))
    return found


def load_supersessions(path: Path = SUPERSESSIONS) -> dict[tuple[str, int], dict]:
    """`{(ledger, row index): entry}`, checked against the digest as well.

    **Keyed by ledger as well as by index since Task 47**, because the check now
    spans several ledgers and row 7 of one is not row 7 of another. An entry
    without a `ledger` field means the main ledger, which is what every entry
    written before this meant -- there were none, so nothing needed migrating,
    but the default is the right one either way.
    """
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            entry = json.loads(line)
            out[(entry.get("ledger", DEFAULT_LEDGER_KEY), entry["row"])] = entry
    return out


_ENVIRONMENT: str | None = None


def _current_environment() -> str:
    global _ENVIRONMENT
    if _ENVIRONMENT is None:
        _ENVIRONMENT = integrity.environment_fingerprint()
    return _ENVIRONMENT


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
              superseded: dict[tuple[str, int], dict],
              replay_log: Path | None = None,
              ledger: str = DEFAULT_LEDGER_KEY) -> dict:
    sha = row.get("git_sha", "")
    result = {
        "ledger": ledger,
        "row": index,
        "purpose": row.get("purpose"),
        "profile": row.get("profile"),
        "outcome": row.get("outcome"),
        "git_sha": sha[:12],
        "sha_in_history": sha_is_in_history(sha),
        "has_code_fingerprint": bool(row.get("code_fingerprint")),
        "has_artifacts": bool(row.get("artifacts")),
        "has_fill_log": bool(row.get("fill_log_source")),
        "environment_drifted": None,
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

    # Distinct from superseded, and deliberately so: the row may be perfectly
    # good. It was not produced under this environment, and nobody should expect
    # bit-identical replay across the gap.
    recorded_env = row.get("environment_fingerprint")
    if recorded_env:
        result["environment_drifted"] = recorded_env != _current_environment()
        if result["environment_drifted"]:
            result["notes"].append(
                f"environment differs from today's "
                f"({recorded_env[:12]} then, {_current_environment()[:12]} now); "
                f"replay may not be bit-identical"
            )
    else:
        result["notes"].append("row predates the environment digest (Task 23)")

    if (ledger, index) in superseded:
        entry = superseded[(ledger, index)]
        result["superseded"] = entry.get("reason", "")
        if entry.get("blotter_digest") not in (None, row.get("blotter_digest")):
            result["notes"].append(
                "SUPERSESSION POINTS AT A DIFFERENT ROW: the ledger has shifted "
                "under it and the entry must be re-checked by hand"
            )

    if replay and row.get("blotter_digest") and row.get("purpose") != "replay":
        result["reproduces"] = _replay_reproduces(row, result, replay_log)
    return result


def _replay_reproduces(row: dict, result: dict,
                       replay_log: Path | None = None) -> bool | None:
    from replay import replay as do_replay

    try:
        engine = do_replay(row, replay_log=replay_log)
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
    if not result["git_sha"]:
        # A row that records NO sha at all, as against one that records a sha
        # nobody has. The two were the same verdict until Task 47 and they are
        # not the same condition: the only rows in this repo that hit it are
        # blocked proposals -- the model call failed, so no strategy ever ran
        # and `run.py` was never reached to stamp the commit. They still carry
        # `code_fingerprint`, and it is the one every other row in their own
        # campaign carries, so the code state IS recoverable; what is missing is
        # this row's ability to say so by itself.
        #
        # Not folded into the clean set: a trial that counts toward N and cannot
        # name its commit is worth a human seeing. Not in the failing set
        # either, because there is no run here to fail to reproduce.
        return "no-sha"
    if not result["sha_in_history"]:
        return "UNVERIFIABLE"
    if result["code_fingerprint_matches"] is False:
        return "unpinned"
    if result.get("environment_drifted"):
        # Not a failure. The row is checkable and may reproduce exactly; it was
        # produced under a different environment and says so.
        return ("env-drifted+reproduces" if result["reproduces"] is True
                else "env-drifted")
    if result["reproduces"] is True:
        return "reproduces"
    return "checkable"


def registered_campaigns_without_a_ledger(root: Path = REPO_ROOT) -> list[Path]:
    """Campaign records whose directory holds no ledger.

    A pre-registration with no rows beside it is either a campaign that never
    ran or a campaign whose rows went somewhere else, and the two look identical
    from here. Reported rather than judged: both are worth a human knowing, and
    only the human knows which it was.
    """
    out = []
    for record in sorted((root / "runs" / "campaigns").rglob("campaign.json")):
        if not (record.parent / "ledger.jsonl").is_file():
            out.append(record.parent)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ledgers", nargs="*", type=Path,
                    help="ledgers to check; every ledger in the repo by default "
                         "-- runs/ledger.jsonl and runs/campaigns/**/ledger.jsonl")
    ap.add_argument("--replay", action="store_true",
                    help="re-run every row and compare digests. Slow, and the "
                         "only check that actually answers the question.")
    ap.add_argument("--supersessions", type=Path, default=SUPERSESSIONS)
    ap.add_argument("--replay-log", type=Path, default=None,
                    help="where each replay's own record goes; "
                         "runs/replays.jsonl by default, never the ledger")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    ledgers = args.ledgers or discover_ledgers()
    if not ledgers:
        print("no ledgers found", file=sys.stderr)
        return 1
    superseded = load_supersessions(args.supersessions)

    per_ledger: list[tuple[Path, list[dict]]] = []
    for path in ledgers:
        key = ledger_key(path)
        rows = load_rows(path)
        per_ledger.append((path, [
            check_row(i, r, replay=args.replay, superseded=superseded,
                      replay_log=args.replay_log, ledger=key)
            for i, r in enumerate(rows)]))

    everything = [r for _, results in per_ledger for r in results]

    if args.json:
        print(json.dumps(everything, indent=2, sort_keys=True))
    else:
        for path, results in per_ledger:
            print(f"{ledger_key(path)}  {len(results)} rows\n")
            print(f"{'row':>3}  {'purpose':<8} {'profile':<14} {'sha':<12} "
                  f"{'verdict':<15} notes")
            for r in results:
                note = "; ".join(r["notes"]) if r["notes"] else ""
                if "superseded" in r:
                    note = (f"superseded: {r['superseded']}"
                            + (f"; {note}" if note else ""))
                print(f"{r['row']:>3}  {r['purpose'] or '-':<8} "
                      f"{r['profile'] or '-':<14} {r['git_sha']:<12} "
                      f"{verdict(r):<15} {note}")
            _summarise(results, indent="  ", replay=args.replay)
            print()

        if len(per_ledger) > 1:
            print("=" * 72)
            print("ALL LEDGERS")
            _summarise(everything, indent="  ", replay=args.replay)
            trials = sum(1 for r in everything
                         if r["purpose"] == "trial" and "superseded" not in r)
            main_trials = sum(
                1 for r in everything
                if r["ledger"] == DEFAULT_LEDGER_KEY
                and r["purpose"] == "trial" and "superseded" not in r)
            print()
            print(f"  trial rows across every ledger: {trials}")
            print(f"  of those, in {DEFAULT_LEDGER_KEY}: {main_trials}")
            if trials != main_trials:
                print()
                print("  These are not the same number, and which one is N is a")
                print("  question this tool does not answer. CLAUDE.md says N is")
                print("  global and counts every trial including the crashed"
                      " ones;")
                print("  the campaign runner writes its rows to the campaign's"
                      " own")
                print("  ledger and never to the main one, so the main ledger"
                      " has")
                print("  never seen them. Decide it deliberately, once.")

        orphans = registered_campaigns_without_a_ledger()
        if orphans:
            print()
            print("registered campaigns with no ledger beside them:")
            for d in orphans:
                print(f"  {d.relative_to(REPO_ROOT)}")
            print("  Either they never ran, or their rows went elsewhere.")

    bad = [r for r in everything
           if verdict(r) in ("UNREPRODUCIBLE", "UNVERIFIABLE")]
    unpinned = [r for r in everything if verdict(r) == "unpinned"]
    no_sha = [r for r in everything if verdict(r) == "no-sha"]
    if no_sha:
        print(f"\n{len(no_sha)} row(s) record no git_sha at all. Every one is a "
              "blocked proposal -- the model call failed, so no strategy ran "
              "and the commit was never stamped -- and every one still counts "
              "toward N, which is the design. Their `code_fingerprint` matches "
              "their campaign siblings', so the code is recoverable that way. "
              "The row writer not stamping a sha on this path is a defect worth "
              "fixing where the row is built, not here.", file=sys.stderr)
    if unpinned:
        print(f"\n{len(unpinned)} row(s) are unpinned: the code they name does "
              "not hash to what they recorded, so they were run against "
              "uncommitted edits and their code state is not recoverable from "
              "history. Not a failure -- the rows replay from stored source -- "
              "but any number derived from them carries that caveat.",
              file=sys.stderr)
    if bad:
        print(f"\n{len(bad)} row(s) cannot be verified. Supersede them with a "
              f"reason in {args.supersessions.name} or fix what broke; do not "
              f"delete them.", file=sys.stderr)
        return 1
    return 0


def _summarise(results: list[dict], *, indent: str, replay: bool) -> None:
    counted = [r for r in results
               if r["purpose"] == "trial" and "superseded" not in r]
    print()
    print(f"{indent}N (purpose=trial, not superseded): {len(counted)}")
    print(f"{indent}superseded: {sum(1 for r in results if 'superseded' in r)}")
    print(f"{indent}unpinned: {sum(1 for r in results if verdict(r) == 'unpinned')}")
    print(f"{indent}no-sha: {sum(1 for r in results if verdict(r) == 'no-sha')}")
    print(f"{indent}environment-drifted: "
          f"{sum(1 for r in results if r.get('environment_drifted'))}"
          f"  (current environment {_current_environment()[:12]})")
    if replay:
        print(f"{indent}reproduces: "
              f"{sum(1 for r in results if r['reproduces'] is True)}"
              f" / {sum(1 for r in results if r['reproduces'] is not None)} attempted")
    else:
        print(f"{indent}reproduction not attempted; re-run with --replay")


if __name__ == "__main__":
    raise SystemExit(main())
