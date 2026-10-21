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


# --------------------------------------------------------------------------- #
# The suite may not talk to a real endpoint.
# --------------------------------------------------------------------------- #
# Measured 2026-10-20, the day the gateway came back: the suite went from 136 s
# to over ten minutes at 0% CPU, sleeping in `TRANSPORT_BACKOFF_S` on retries
# against `llm-proxy.invalid`. Three test helpers used `os.environ.setdefault` for
# `LITELLM_BASE_URL`, so anyone with the gateway exported ran those tests
# against it -- real calls, real tokens, and a suite whose behaviour depended on
# whose shell it was.
#
# Fixing the three helpers fixes those three. This fixture makes the whole class
# of bug impossible to reintroduce: a test that reaches a real host fails, by
# name, in milliseconds, instead of sleeping for twenty-seven minutes and
# passing. Tests marked `live` are exempt -- reading the bucket is their entire
# point -- and they already skip without R2 credentials.
_ALLOWED_HOSTS = frozenset({"example.invalid", "localhost", "127.0.0.1", "::1"})


@pytest.fixture(autouse=True)
def _no_real_network(request, monkeypatch):
    if "live" in request.node.keywords:
        return
    import urllib.parse

    import requests.sessions

    real = requests.sessions.Session.request

    def guarded(self, method, url, *a, **kw):
        host = (urllib.parse.urlparse(str(url)).hostname or "").lower()
        if host not in _ALLOWED_HOSTS:
            _rec = os.environ.get("NETGUARD_LOG")
            if _rec:
                with open(_rec, "a") as fh:
                    fh.write(f"{request.node.nodeid}\t{method}\t{url}\n")
            raise RuntimeError(
                f"{request.node.nodeid} tried to reach {host!r} "
                f"({method} {url}). Tests do not call real endpoints: point the "
                f"client at example.invalid, or mark the test `live`.")
        return real(self, method, url, *a, **kw)

    monkeypatch.setattr(requests.sessions.Session, "request", guarded)
