"""Collection gating for the aider-factory RAG end-to-end tests.

These are live integration tests, not offline unit tests. They shell out to the
``aider-oracle`` and ``aider-validate`` CLIs, reach a MiniCheck NLI server, and
pull the ``BAAI/bge-m3`` embedding model. Without that toolchain the tests cannot
run at all, so they are skipped when the CLIs are missing -- the same treatment
the R2 ``live`` tests get when ``R2_*`` is absent. ``pytest -q`` stays the whole
suite and stays green.

To run them::

    pip install aider-factory
    export GRINDING_AGENT_API_BASE=http://<minicheck-host>:8090/v1
    pytest tests/end-to-end -q

See ``docs/aider_factory_rag_guide.md`` for the full startup sequence.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

REQUIRED_CLIS = ("aider-oracle", "aider-validate")


def _missing_clis() -> list[str]:
    return [cli for cli in REQUIRED_CLIS if not shutil.which(cli)]


def _in_this_dir(item: pytest.Item) -> bool:
    """True only for tests collected under ``tests/end-to-end/``.

    ``pytest_collection_modifyitems`` receives every item in the run, not just
    this directory's, so the filter has to be explicit.
    """
    path = Path(str(getattr(item, "path", None) or item.fspath)).resolve()
    return HERE in path.parents


def pytest_collection_modifyitems(config, items):
    missing = _missing_clis()
    if not missing:
        return

    reason = (
        "requires aider-factory CLIs on PATH: "
        + ", ".join(missing)
        + " (pip install aider-factory); also needs a reachable MiniCheck "
        "endpoint via GRINDING_AGENT_API_BASE"
    )
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if _in_this_dir(item):
            item.add_marker(skip)
