"""No engine without a ledger row -- structurally, not by everyone remembering.

Until Task 23 it was a convention that every execution left a record: `run.py`
appended one, `scripts/nq_smoke.py` appended one, and a new entry point that
forgot would simply not have appended one. The ledger is the denominator of every
false-discovery statement this project makes, so "we always remember" is a
guarantee resting on the memory of whoever writes the next script.

`Engine.__init__` now requires a `RunTicket`, and there are exactly three ways to
mint one. Each says what kind of accounting the run gets, and the kinds are not
interchangeable:

- **`LedgerRun`** -- a context manager that appends exactly one row on exit,
  whatever happened inside it, and hands out the ticket for the engine. This is
  the scored path and every evaluation goes through it.
- **`RunTicket.for_calibration`** -- the null tabulation runs thousands of
  engines against the *immutable reference strategy* to measure the apparatus.
  Those are not evaluations of a candidate and ledgering each would drown the
  record it exists to keep. One row for the tabulation, written by
  `scripts/tabulate_null.py`, not one per engine. The brief for this work did not
  anticipate this third kind; it is called out rather than folded into one of the
  other two, and a test pins where it may be used.
- **`RunTicket.for_test`** -- fixtures. A test asserts it appears nowhere under
  `src/` or `scripts/`.

**This stops accidents, not attackers.** Anything running in this process can
reach `_MINT` and forge a ticket, exactly as it can reach anything else in the
address space; that is the same in-process boundary every other guard here runs
into, and the answer is still a separate process. What it does stop is the
failure that actually happened -- a legitimate entry point added without a
thought for the ledger -- and `tests/test_boundary.py` walks the tree for
construction sites so a fourth route cannot appear quietly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ._roots import repo_root

# Was `Path(__file__).resolve().parents[2]`. The nine modules under
# src/core/ counted at three different depths, which is the evidence
# that a count describes this file rather than the repository.
REPO_ROOT = repo_root()
STRATEGY_PATH = REPO_ROOT / "src" / "strategies" / "current.py"
# The one strategy the harness may run in its own process. Not because it is
# trusted in some general sense, but because it is *committed*: its digest is in
# git, the CI path check refuses edits to it from the agent, and calibration runs
# tens of thousands of engines against it to measure the apparatus. Paying
# process spawn on each of those is not sensible and the reference is not the
# thing this distrusts.
REFERENCE_PATH = (REPO_ROOT / "src" / "core" / "scoring" / "calibration"
                  / "reference.py")

# See the docstring: a sentinel, not a secret.
_MINT = object()


class NotTheReferenceStrategy(RuntimeError):
    """Something other than the committed reference asked to run in process.

    Findings 2 and 3 of the second adversarial pass are both "strategy code and
    engine code share an address space". A single in-process exception for
    arbitrary code puts them both back, so the exception is bound to a digest
    rather than to an intention.
    """


class RunNotAccounted(RuntimeError):
    """An engine was constructed without a ticket, or with a forged one.

    Raised at construction rather than at `run()`, so the failure lands on the
    line that forgot rather than three frames into a walk.
    """


@dataclass(frozen=True, slots=True)
class RunTicket:
    """Proof that this engine run is accounted for somewhere."""

    kind: str          # "ledger" | "calibration" | "test"
    reason: str = ""
    # Checked in `__post_init__` so `RunTicket(kind="ledger")` raises rather than
    # producing something the engine accepts. A field rather than a check inside
    # the classmethods, because the classmethods are not the only way to call a
    # dataclass.
    key: object = None

    def __post_init__(self) -> None:
        if self.key is not _MINT:
            raise RunNotAccounted(
                "a RunTicket is minted by LedgerRun, RunTicket.for_calibration or "
                "RunTicket.for_test, never by calling RunTicket(...) directly. "
                "Every engine run has to be accounted for somewhere."
            )
        if self.kind not in ("ledger", "calibration", "test"):
            raise RunNotAccounted(f"unknown ticket kind {self.kind!r}")

    @classmethod
    def _mint(cls, key, kind: str, reason: str = "") -> RunTicket:
        return cls(kind=kind, reason=reason, key=key)

    @classmethod
    def for_calibration(cls, reason: str,
                        strategy_path: Path | None = None) -> RunTicket:
        """Measuring the apparatus with the immutable reference strategy.

        Not an evaluation of a candidate, and deliberately not one row each: the
        null is tens of thousands of engines and the ledger would stop being
        readable. `reason` is required so a caller has to say which calibration.

        **This is also the only in-process route.** Since Task 25 a strategy runs
        in its own process; the exception is a strategy whose source digest
        equals the committed reference's, checked here rather than promised in a
        comment. A calibration ticket for anything else is refused, so the rule
        cannot be worked around by passing `for_calibration` a different
        strategy -- which is the shape the loophole would take.
        """
        if not reason:
            raise RunNotAccounted("for_calibration() needs a reason")
        path = Path(strategy_path) if strategy_path else REFERENCE_PATH
        if strategy_digest(path) != strategy_digest(REFERENCE_PATH):
            raise NotTheReferenceStrategy(
                f"{path} is not the committed reference strategy "
                f"({strategy_digest(path)} vs {strategy_digest(REFERENCE_PATH)}). "
                "In-process execution is permitted only for the reference; "
                "everything else runs in its own process. See "
                "src/core/engine/remote/."
            )
        return cls._mint(_MINT, "calibration", reason)

    @classmethod
    def for_null_calibration(cls, reason: str) -> RunTicket:
        """Calibrating a null against *arbitrary* candidate code.

        A third calibration route, and it exists because Task 31's per-candidate
        null does something `for_calibration` deliberately forbids: it runs
        strategies that are not the committed reference. Thousands of them, to
        build a null.

        So it does not check a digest, and in exchange **it does not permit
        in-process execution**. A caller minting this ticket is running untrusted
        code and has to run it behind the boundary; `tests/test_accounting.py`
        asserts every call site pairs it with `RemoteStrategy`. The digest rule
        and the isolation rule are two ways of answering the same question, and
        this route takes the second one.
        """
        if not reason:
            raise RunNotAccounted("for_null_calibration() needs a reason")
        return cls._mint(_MINT, "calibration", f"null calibration: {reason}")

    @classmethod
    def for_test(cls, reason: str = "fixture") -> RunTicket:
        """Fixtures only. Asserted absent from `src/` and `scripts/`."""
        return cls._mint(_MINT, "test", reason)


# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #
# Moved here from `run.py` in Task 23 so that the thing which appends a row and
# the thing which authorises an engine are one object. Split across two modules
# they were two habits.
MAX_STORED_FILLS = 250_000


# --------------------------------------------------------------------------- #
# N: campaign-scoped for the correction, global for honesty
# --------------------------------------------------------------------------- #
#
# **These are two different numbers and conflating them was a real defect.**
#
# The *correction* is campaign-scoped and always was in practice: `max_of_n`
# takes the maximum over that campaign's per-candidate nulls, and
# `deflation_view(best, len(trials), ...)` is handed the campaign's trial count.
# A bar computed from every trial the project has ever run would be a bar for a
# family nobody selected over.
#
# The *global* count is the honesty number -- how many times has this project
# looked. It is not a bar and nothing computes one from it.
#
# CLAUDE.md said "any deflated-Sharpe or Harvey threshold computed anywhere uses
# the global count", which was the Benjamini-Hochberg framing: one p-value
# column, one family, one denominator. BH stopped being the gate when the null
# became per-candidate and conditional (Tasks 31-33; `campaign.py` names Task 32
# as the demotion), and the sentence was never updated.
#
# **The global count is a UNION and is derived, never stored.** Campaign trials
# are written to the campaign's own ledger and never to `runs/ledger.jsonl`, so
# for a long time the main ledger reported N = 0 while 400 trials sat beside it.
# Deriving rather than dual-writing is deliberate: there is nothing to keep in
# sync, no dedup question, no 400-row backfill appending historical rows under
# today's timestamps, and past and future are handled by the same rule.

LEDGER_NAME = "ledger.jsonl"
SUPERSESSIONS_NAME = "supersessions.jsonl"


def ledger_paths(root: Path | None = None) -> list[Path]:
    """Every ledger in the repository, the main one first.

    `runs/replays.jsonl` is not one: a replay is not a trial and carries its own
    purpose. `docs/reviews/**` is not either -- a task's scratch output is not
    the project's record, and counting it would let writing a review move N.
    """
    root = root or REPO_ROOT
    found = []
    main = root / "runs" / LEDGER_NAME
    if main.is_file():
        found.append(main)
    found.extend(sorted((root / "runs" / "campaigns").rglob(LEDGER_NAME)))
    return found


def superseded_rows(root: Path) -> set[tuple[str, int]]:
    """Rows removed from N by the append-only supersession record."""
    path = root / "runs" / SUPERSESSIONS_NAME
    if not path.is_file():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        ledger = entry.get("ledger", "runs/ledger.jsonl")
        row = entry.get("row")
        if isinstance(ledger, str) and isinstance(row, int):
            out.add((ledger, row))
    return out


def trial_rows(root: Path | None = None) -> list[tuple[str, int, dict]]:
    """`(ledger, index, row)` for non-superseded rows with `purpose == "trial"`.

    The ledger is named because a row's identity is its position in a specific
    file; row 7 of one is not row 7 of another, and a supersession entry has to
    be able to say which.
    """
    root = root or REPO_ROOT
    superseded = superseded_rows(root)
    out = []
    for path in ledger_paths(root):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        for i, line in enumerate(path.read_text().splitlines()):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("purpose") == "trial" and (rel, i) not in superseded:
                out.append((rel, i, row))
    return out


def global_n(root: Path | None = None) -> int:
    """How many times this project has looked. Not a bar; nobody computes one.

    Includes crashed and abandoned trials, and blocked proposals. A row is
    excluded only by an append-only supersession record naming its ledger row
    and reason; a platform failure is not evidence about a proposal.
    """
    return len(trial_rows(root))


def append_ledger(path: Path, record: dict) -> None:
    """Append-only. One line per invocation, whatever the outcome."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def strategy_digest(strategy_path: Path | None = None) -> str:
    """Content hash of the strategy source.

    `git_sha` alone does not identify a trial: the agent edits
    `src/strategies/current.py` and runs it before anything is committed, so two
    trials share a sha and differ entirely.
    """
    strategy_path = strategy_path or STRATEGY_PATH
    if not strategy_path.exists():
        return ""
    return hashlib.sha256(strategy_path.read_bytes()).hexdigest()[:16]


def store_source(ledger_path: Path, digest: str,
                 strategy_path: Path | None = None) -> str:
    """Keep the strategy source beside the ledger, addressed by its digest."""
    strategy_path = strategy_path or STRATEGY_PATH
    if not strategy_path.exists():
        return ""
    out = ledger_path.parent / "sources" / f"{digest}.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        out.write_text(strategy_path.read_text())
    try:
        return str(out.relative_to(REPO_ROOT))
    except ValueError:
        return str(out)


def store_weights(ledger_path: Path, digest: str, strategy) -> str:
    """Keep a fitted strategy's weights beside the ledger, addressed by its digest.

    Mirrors `store_source`: same directory convention, same digest, same relative
    path returned. `runs/weights/` for a non-campaign run, and
    `runs/campaigns/<id>/weights/` for a trial, because the ledger path differs and
    nothing else has to know.

    **The weights are half the strategy.** A fitted strategy's source describes how
    to fit; the weights are what it fitted to. Hashing only the source would let two
    runs with identical code and different training hash the same, which is the
    failure `code_fingerprint` and `artifact_digests` exist to prevent everywhere
    else. See `docs/design/ml-strategies.md`.

    Returns "" for a strategy that is not fitted, or one whose `save_weights` wrote
    nothing -- a missing file is recorded as absent rather than as an empty one.
    """
    save = getattr(strategy, "save_weights", None)
    if save is None or not digest:
        return ""
    out = ledger_path.parent / "weights" / f"{digest}.weights"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        save(out)
    if not out.exists():
        return ""
    try:
        return str(out.relative_to(REPO_ROOT))
    except ValueError:
        return str(out)


def store_fill_log(ledger_path: Path, digest: str, fills) -> str:
    """Keep the run's fills beside the ledger, addressed by their digest."""
    import gzip

    if not fills or len(fills) > MAX_STORED_FILLS:
        return ""
    out = ledger_path.parent / "fills" / f"{digest}.jsonl.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        with gzip.open(out, "wt") as fh:
            for fill in fills:
                fh.write(json.dumps(fill.as_row()) + "\n")
    try:
        return str(out.relative_to(REPO_ROOT))
    except ValueError:
        return str(out)


@dataclass
class LedgerRun:
    """A ledger row that will be written, and the ticket that authorises the run.

        with LedgerRun(path, row_builder=build) as run_log:
            engine = Engine(dataset, strategy, ticket=run_log.ticket, ...)
            engine.run()
            run_log.update(engine.record.as_dict())

    The row is appended in `__exit__`, on every path out -- return, raise, or a
    `sys.exit` from inside. A run that crashed is a run that happened, and the
    ledger's whole claim is that it holds those too.

    `row_builder` turns the accumulated record dict into the final row. It is
    injected rather than imported so this module does not have to know about
    fingerprints, profiles or `git_sha`, which are `run.py`'s business.
    """

    ledger_path: Path
    row_builder: object                     # (record) -> dict
    record: dict = field(default_factory=dict)
    written: bool = False
    _ticket: RunTicket | None = None

    def __enter__(self) -> "LedgerRun":
        self._ticket = RunTicket._mint(_MINT, "ledger")
        return self

    @property
    def ticket(self) -> RunTicket:
        if self._ticket is None:
            raise RunNotAccounted(
                "the ticket exists only inside the `with` block, because that is "
                "the only span in which the row is guaranteed to be written"
            )
        return self._ticket

    def update(self, record: dict) -> None:
        """What the row should say. Called as the run learns things; the last
        call before exit wins, and no call at all still writes a row."""
        self.record = dict(record)

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self.written:
            self.written = True
            append_ledger(self.ledger_path, self.row_builder(self.record))
        return False        # never swallow: an invalidated trial is still a raise
