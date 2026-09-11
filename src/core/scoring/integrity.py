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
        out.append(f"{name}|const|{obj!r}"[:400])


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
