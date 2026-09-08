"""Fixtures for the data-integrity regression tests.

Two tiers:

* offline tests read `data/manifest.json` and `config/corrections.yaml`, both
  committed, so they run anywhere including CI with no credentials;
* tests marked `live` re-read the bucket and are skipped unless the R2
  environment is present.

The offline tier is not a weaker copy of the live one. The manifest is generated
from the bucket by `scripts/inventory.py` and committed, so a regression reaches
CI as a diff in the manifest and the offline assertions fail on it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

R2_ENV = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs read access to the R2 bucket")


def pytest_collection_modifyitems(config, items):
    if all(os.environ.get(v) for v in R2_ENV):
        return
    skip = pytest.mark.skip(reason="R2 credentials not in the environment")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def manifest() -> dict:
    path = REPO_ROOT / "data" / "manifest.json"
    assert path.exists(), "data/manifest.json is missing; run scripts/inventory.py"
    return json.loads(path.read_text())


@pytest.fixture(scope="session")
def corrections_doc() -> dict:
    path = REPO_ROOT / "config" / "corrections.yaml"
    assert path.exists(), "config/corrections.yaml is missing"
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="session")
def corrections(corrections_doc) -> list[dict]:
    return corrections_doc.get("corrections") or []


@pytest.fixture(scope="session")
def s3():
    import inventory as inv

    return inv.s3_client(8)


@pytest.fixture(scope="session")
def bucket() -> str:
    return os.environ["R2_BUCKET"]
