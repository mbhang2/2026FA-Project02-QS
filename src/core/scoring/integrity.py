"""Did the scoring code change while the trial was running?

The import guards in `scorer.py`, `metrics.py` and `calibration/__init__.py`
refuse an import from `src.strategies` by walking the import stack. Adversarial
review showed what that is worth: `run.py` imports the scorer before it loads the
strategy, so the module is already in `sys.modules` and the module-level guard
never runs again. A strategy can then `import src.core.scoring.scorer` and the
guard is silent -- and the static source check is bypassable with `__import__`, a
constructed name, or `sys.modules` directly.

**The threat is not reading, it is writing.** The metric formula is public; it is
in `docs/design/metric.md`. What matters is that a strategy which can reach the
module can replace a function in it, and every subsequent harness call then
returns whatever it likes -- a fabricated score, in the one place the loop trusts
without checking.

So this is the layer that does not depend on an import being observed. The
scoring code is fingerprinted before the run and again after scoring, and any
difference invalidates the trial. **Detect and record rather than only prevent**
is this project's idiom everywhere else and it applies here: the fingerprint
catches mutations no static check can see, including ones arriving through paths
nobody has thought of.

It is not a sandbox. A caller determined enough to restore the original functions
after using them would pass, and nothing in-process can stop that -- the boundary
is a separate process, and this is what stands in for it until there is one.
Stated plainly because the previous arrangement implied a boundary it did not
have.
"""

from __future__ import annotations

import hashlib
import sys
from types import FunctionType, ModuleType

# Everything whose behaviour the trial's number depends on.
WATCHED_PREFIXES = ("src.core.scoring",)


def _code_digest(obj, out: list[str], seen: set[int], name: str) -> None:
    """Fold one attribute into the fingerprint.

    Bytecode rather than source: monkeypatching does not touch a file, so hashing
    the file on disk would report everything as unchanged while the running
    module returned something else entirely.
    """
    if id(obj) in seen:
        return
    seen.add(id(obj))
    if isinstance(obj, FunctionType):
        code = obj.__code__
        out.append(
            f"{name}|f|{code.co_name}|{code.co_argcount}|"
            f"{hashlib.sha256(code.co_code).hexdigest()}|"
            f"{sorted(code.co_names)}|{len(code.co_consts)}"
        )
        # A replacement that closes over different values is still a
        # replacement, and closure cells are where a plausible forgery would
        # hide its constants.
        for cell in obj.__closure__ or ():
            try:
                out.append(f"{name}|cell|{type(cell.cell_contents).__name__}")
            except ValueError:
                out.append(f"{name}|cell|empty")
    elif isinstance(obj, type):
        for attr in sorted(vars(obj)):
            if attr.startswith("__") and attr not in ("__init__", "__call__"):
                continue
            _code_digest(vars(obj)[attr], out, seen, f"{name}.{attr}")
    elif isinstance(obj, (staticmethod, classmethod)):
        _code_digest(obj.__func__, out, seen, name)
    elif isinstance(obj, (int, float, str, bool, bytes, tuple, frozenset)):
        # Hashed, not truncated. This used to be `f"...{obj!r}"[:400]`, which
        # meant a constant longer than 400 characters was fingerprinted by its
        # first 400 -- so the threshold grids, which are exactly the long
        # constants worth protecting, were covered up to a prefix and no
        # further. A digest that silently stops looking is the same defect
        # `Blotter.digest`'s rounding turned out to be, found by auditing for it
        # rather than by being bitten twice.
        rendered = repr(obj)
        out.append(f"{name}|const|{len(rendered)}|"
                   f"{hashlib.sha256(rendered.encode()).hexdigest()}")


def watched_modules() -> list[str]:
    return sorted(
        name
        for name, mod in list(sys.modules.items())
        if isinstance(mod, ModuleType)
        and any(name == p or name.startswith(p + ".") for p in WATCHED_PREFIXES)
    )


def fingerprint() -> str:
    """A hash of the scoring code as it is currently loaded.

    Constants are included as well as bytecode: `MIN_SESSIONS`, `REJECT` and the
    threshold grids decide outcomes as surely as the functions do, and rebinding
    one is a cheaper forgery than rewriting a function.
    """
    out: list[str] = []
    seen: set[int] = set()
    for name in watched_modules():
        module = sys.modules[name]
        out.append(f"module|{name}")
        for attr in sorted(vars(module)):
            if attr.startswith("_"):
                continue
            value = vars(module)[attr]
            if isinstance(value, ModuleType):
                continue
            _code_digest(value, out, seen, f"{name}.{attr}")
    return hashlib.sha256("\n".join(out).encode()).hexdigest()


class ScoringMutated(RuntimeError):
    """The scoring code changed during a trial, so the trial's number is not the
    harness's number. Raised rather than reported so the run cannot complete
    quietly with a score nobody can vouch for -- and the ledger row is written
    either way, because an invalidated trial still happened and still counts."""


def check(before: str, after: str) -> None:
    if before != after:
        raise ScoringMutated(
            "the scoring code changed while the trial was running: "
            f"{before[:12]} -> {after[:12]}. The score this run produced was not "
            "produced by the committed metric. The trial is recorded and "
            "invalidated; nothing about it should be believed."
        )


# --------------------------------------------------------------------------- #
# the artifacts a result depends on
# --------------------------------------------------------------------------- #
# `fingerprint()` above watches scoring *modules*. Adversarial review's finding 7
# is that this is the wrong set: rewriting `data/calibration/empirical-null.json`
# moves every accept/reject decision in the harness and does not change a single
# byte of bytecode. The fingerprint reported everything as unchanged, which is
# worse than not having one, because the run record then carries an assurance it
# did not earn.
#
# So there is a second fingerprint over the *files* a number depends on. Two
# fingerprints rather than one extended fingerprint, because they answer
# different questions and fail for different reasons: one says the code running
# in this process is the committed code, the other says the tables and calendars
# on disk are the ones the row claims.
#
# **This is detection, not prevention.** A strategy can still write to any of
# these paths -- they are ordinary files and it shares a filesystem. What it
# cannot do is write to them and have the result stand. Prevention is a separate
# process with a read-only mount, and that is a later task.
from pathlib import Path  # noqa: E402

from .._roots import repo_root

# Was `Path(__file__).resolve().parents[3]`. The nine modules under
# src/core/ counted at three different depths, which is the evidence
# that a count describes this file rather than the repository.
REPO_ROOT = repo_root()

# Fixed here rather than assembled by a caller. A list passed in is a list a
# caller can shorten, and the caller closest to the incentive is the one running
# the strategy.
ARTIFACT_GLOBS = (
    "config/*.yaml",                 # run profiles, corrections
    "config/calendars/*.yaml",       # the pre-registered splits
    "config/prompts/*.md",           # the proposing agent's prompt (Task 36):
                                     # an artifact that shaped the result, the
                                     # same way a null table is
    "config/hypotheses/*.md",        # the campaign's question (Task 46). In for
                                     # the identical reason: two campaigns run
                                     # under different hypotheses are different
                                     # experiments, and their scores are not
                                     # comparable. The file's commit date is
                                     # the pre-registration, as for a calendar.
    "data/calibration/*.json",       # every null table, tagged or not
    "data/manifest.json",            # the bucket fingerprint
    "data/nq-contract-selection.json",
    "data/nq-session-closes.json",
    "data/dbn-version-equivalence.json",
    "src/strategies/current.py",     # the strategy's own source, at load and after
)

# Deliberately not in the set, each for a reason rather than by omission:
#
#   raw/ in the bucket   immutable by policy and read-only by credential; the
#                        manifest is its fingerprint and the manifest is in.
#   data/cache/          a derived local cache of bucket objects, 1.1 GiB, and
#                        keyed by content. Rewriting it to change a result would
#                        change `dataset_digest`, which is already in the row.
#   runs/                the ledger is output, not input. Its integrity is a
#                        different problem and `scripts/ledger_check.py` is it.
#   docs/                a result does not depend on prose. The tests that pin
#                        docs to code are in CI, which is where that belongs.
#   the environment      numpy and pandas versions change numbers and are not
#                        covered here. Named because it is a real gap and the
#                        honest place for it is a lockfile, not a hash.
#
# The engine and scoring *sources* are not here either -- they are
# `code_fingerprint()` below, which is a separate field because it answers "was
# this row produced by code still in history" rather than "did the tables move
# under the run".
SOURCE_GLOBS = (
    "src/core/**/*.py",
    "run.py",
)


class ArtifactsMutated(RuntimeError):
    """A file the result depends on changed while the trial was running.

    Raised rather than reported, for the same reason `ScoringMutated` is: a run
    that completes quietly with a number nobody can attribute to a set of inputs
    is the failure this is meant to make impossible. The row is written either
    way -- an invalidated trial still happened and still counts toward N.
    """


def _digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve(globs: tuple[str, ...], root: Path | None = None) -> list[Path]:
    root = root or REPO_ROOT
    out: set[Path] = set()
    for pattern in globs:
        out.update(p for p in root.glob(pattern) if p.is_file())
    return sorted(out)


def artifact_digests(root: Path | None = None) -> dict[str, str]:
    """`{repo-relative path: sha256}` for every artifact a result depends on.

    A missing file is recorded as `"absent"` rather than skipped. Deleting a null
    table and creating it again with different contents would otherwise look
    exactly like a table that was always there.
    """
    root = root or REPO_ROOT
    found = {str(p.relative_to(root)): _digest_file(p) for p in _resolve(ARTIFACT_GLOBS, root)}
    for pattern in ARTIFACT_GLOBS:
        if "*" not in pattern and pattern not in found:
            found[pattern] = "absent"
    return dict(sorted(found.items()))


def artifact_fingerprint(digests: dict[str, str] | None = None,
                         root: Path | None = None) -> str:
    """One hash over the whole set, for a compact field and a cheap comparison."""
    digests = artifact_digests(root) if digests is None else digests
    payload = "\n".join(f"{name}|{digest}" for name, digest in sorted(digests.items()))
    return hashlib.sha256(payload.encode()).hexdigest()


def code_fingerprint(root: Path | None = None) -> str:
    """A hash over the engine, data and scoring *sources* as they are on disk.

    Source rather than bytecode, and files rather than modules, because this one
    has to be recomputable at an arbitrary commit: `scripts/ledger_check.py` asks
    whether a row's fingerprint is reachable in history, and the only way to
    answer that is to recompute it from what history holds. `fingerprint()`
    remains the one that catches monkeypatching, which no file hash can see.
    """
    root = root or REPO_ROOT
    payload = "\n".join(
        f"{p.relative_to(root)}|{_digest_file(p)}" for p in _resolve(SOURCE_GLOBS, root)
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def check_artifacts(before: dict[str, str], after: dict[str, str]) -> None:
    if before == after:
        return
    changed = sorted(
        set(before) | set(after),
        key=lambda k: (before.get(k) == after.get(k), k),
    )
    moved = [k for k in changed if before.get(k) != after.get(k)]
    detail = "; ".join(
        f"{k}: {before.get(k, 'absent')[:12]} -> {after.get(k, 'absent')[:12]}"
        for k in moved
    )
    raise ArtifactsMutated(
        "a file the result depends on changed while the trial was running: "
        f"{detail}. The score this run produced was not produced by the "
        "artifacts the row names. The trial is recorded and invalidated."
    )


# --------------------------------------------------------------------------- #
# the environment
# --------------------------------------------------------------------------- #
# The gap Task 22 named and could not close with a hash. numpy and pandas
# versions change numbers, and no digest over the repo's own files can see that.
#
# Two digests rather than one, because they answer different questions:
#
#   lockfile   what the repo *declares* the environment to be. Committed, so a
#              row naming a lockfile digest can be matched against history the
#              way `code_fingerprint` can.
#   resolved   what was actually importable when the run happened, from
#              `importlib.metadata`. This is the one that catches a machine whose
#              pandas was upgraded without the lockfile being regenerated.
#
# A row carrying both says which environment was promised and which was used, and
# `scripts/ledger_check.py` reports a row whose resolved digest differs from
# today's as **environment-drifted** -- which is not superseded. The row may be
# perfectly good; it just was not produced under this environment and nobody
# should expect bit-identical replay across the gap.
LOCKFILE = REPO_ROOT / "requirements.lock"


def lockfile_digest(root: Path | None = None) -> str:
    path = (root or REPO_ROOT) / "requirements.lock"
    return _digest_file(path) if path.exists() else "absent"


def resolved_environment() -> dict[str, str]:
    """`{distribution: version}` for everything importable, plus the interpreter.

    Distribution names are lower-cased and normalised the way the packaging
    ecosystem does, so `PyYAML` and `pyyaml` are one entry and not two.
    """
    import platform
    from importlib.metadata import distributions

    out: dict[str, str] = {}
    for dist in distributions():
        name = (dist.metadata["Name"] or "").strip().lower().replace("_", "-")
        if name:
            out[name] = dist.version
    out["python"] = platform.python_version()
    out["python_implementation"] = platform.python_implementation()
    return dict(sorted(out.items()))


def environment_fingerprint(resolved: dict[str, str] | None = None) -> str:
    resolved = resolved_environment() if resolved is None else resolved
    payload = "\n".join(f"{k}=={v}" for k, v in sorted(resolved.items()))
    return hashlib.sha256(payload.encode()).hexdigest()
