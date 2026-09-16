"""The build-time / run-time line, asserted rather than documented.

`scripts/` produces the committed artifacts -- `data/manifest.json`, the published
session list, `data/nq-contract-selection.json`, the tabulated null,
`docs/data-inventory.md`. `src/core/` consumes them during a trial.

Keeping the producers out of the trial's import graph is what makes those artifacts
authoritative. If `inventory.py` were reachable from `src.core`, a trial could
re-derive coverage and quietly disagree with the manifest it was supposed to be
measured against. If `calibrate.py` or `tabulate_null.py` were, a trial could
tabulate the null it is judged by -- marking its own homework.

Two checks, because they fail differently. The static one reads the source and
catches an import that was written but never executed (a lazy import inside a
function nothing in CI calls). The runtime one imports everything for real and
catches an import the static one cannot see: a `sys.path` insertion followed by a
bare `import inventory`, which is what the shim at the top of every script does and
what someone would plausibly copy into `src/core/`.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE = REPO_ROOT / "src" / "core"
SCRIPTS = REPO_ROOT / "scripts"


def core_modules() -> list[str]:
    """Every importable module under `src.core`, dotted."""
    out = []
    for path in sorted(CORE.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        out.append(".".join(parts))
    return out


def test_the_core_tree_is_not_empty():
    """A guard on the guards: both tests below pass vacuously if the glob is
    wrong, and a restructure is exactly when a glob goes wrong."""
    mods = core_modules()
    assert len(mods) >= 10, mods
    assert "src.core.scoring.metrics" in mods
    assert "src.core.engine.engine" in mods
    assert "src.core.data.dataset" in mods


def test_no_module_under_core_names_a_script_statically():
    offenders = []
    script_names = {p.stem for p in SCRIPTS.glob("*.py")}
    for path in sorted(CORE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import; it cannot escape src/core.
                names = [node.module or ""] if node.level == 0 else []
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root == "scripts" or root in script_names:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {name}")
    assert not offenders, (
        "src/core is run-time and must not import build-time code:\n  "
        + "\n  ".join(offenders)
    )


def test_importing_all_of_core_pulls_in_nothing_from_scripts():
    """Import every core module in one fresh interpreter, then look at what
    actually landed in `sys.modules` -- by file location, not by name, so a
    `sys.path` hack that imports `inventory` as a top-level module is caught."""
    probe = (
        "import importlib, sys\n"
        f"mods = {core_modules()!r}\n"
        "for m in mods:\n"
        "    importlib.import_module(m)\n"
        f"scripts_dir = {str(SCRIPTS)!r}\n"
        "bad = sorted(\n"
        "    name for name, mod in sys.modules.items()\n"
        "    if getattr(mod, '__file__', None)\n"
        "    and str(mod.__file__).startswith(scripts_dir)\n"
        ")\n"
        "print('\\n'.join(bad))\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip() == "", (
        "importing src.core reached build-time code:\n" + r.stdout
    )


def test_scripts_may_import_core_the_other_way_round():
    """The line is directional, not a wall. Asserting it here stops a later
    'fix' from making the dependency symmetric and breaking every script."""
    r = subprocess.run(
        [sys.executable, "-c",
         "import runpy, sys; sys.argv=['inventory.py','--help']; "
         "runpy.run_path('scripts/inventory.py', run_name='__main__')"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    # --help exits 0 via SystemExit; the point is that the imports resolved.
    assert "ModuleNotFoundError" not in r.stderr, r.stderr[-2000:]
