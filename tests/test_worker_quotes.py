"""A parallel worker must be able to read the amendment that unlocks quoted backtests.

`run.py` imports `scripts/prereg.py` before a quoted run. Workers did not carry it, so
all 20 trials of btc-cvd-01 stopped before their first tick with no ledger row.
"""

from __future__ import annotations

import importlib.util
import sys

REPO_SCRIPTS = __import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_a_worker_reads_amendment_20_from_its_own_copy(tmp_path, monkeypatch):
    tree = _load(REPO_SCRIPTS / "worker_tree.py", "worker_tree_under_test")
    monkeypatch.setattr(tree, "WORKERS_DIR", tmp_path / "workers")
    worker = tree.create(1, quiet=True)[0]
    prereg = worker / "scripts" / "prereg.py"
    document = worker / "docs" / "design" / "pre-registration.md"
    assert prereg.is_file() and not prereg.is_symlink()
    assert document.is_file() and not document.is_symlink()
    copy = _load(prereg, "prereg_in_worker")
    assert copy.PREREG == document.resolve()
    assert copy.in_force(20) is True
