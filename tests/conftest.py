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


# --------------------------------------------------------------------------- #
# The suite may not run while a campaign is running.
# --------------------------------------------------------------------------- #
# **Not caution: a shared mutable path.** `src/strategies/current.py` is one
# hardwired location. A campaign writes each candidate there, runs it, and
# restores the original -- and this suite touches the same file BOTH ways:
# `tests/test_task7_interface.py` writes `STRATEGY_PATH`, and
# `tests/test_remote.py` reads it. So running them together can make a test
# read a campaign candidate, or make a trial score the suite's file while the
# ledger records the candidate's digest. The second is the one that matters:
# "the ledger would look clean and be wrong", which is what
# `scripts/worker_tree.py` exists to prevent. `nq_campaign.py` at W>1 has
# worker trees; `positive_control.py` has none at all.
#
# Measured 2026-10-22, by doing it: the full suite was run while
# positive-control-deepseek-v4-pro arm s030 was at 39 of 100, and
# `test_the_uniform_golden_set_holds_through_the_boundary` failed on two
# parameterisations with mismatched fill-log digests. Those were not defects --
# they were a test reading a trial's strategy. Whether a trial also scored the
# wrong source could not be established from the ledger, because every stored
# source still matched its recorded digest, which is exactly what the bad case
# also looks like. `scripts/ledger_check.py --replay` is the only thing that
# can tell, and it costs a full re-walk of the data.
#
# One refusal is cheaper than that, so this is a refusal.
_CAMPAIGN_SCRIPTS = ("nq_campaign.py", "positive_control.py",
                     "research_session.py", "rehearse.py", "llm_campaign.py")


def _running_campaigns() -> list[str]:
    import subprocess
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,command="],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:                                            # noqa: BLE001
        return []                 # cannot tell: do not block the suite on it
    hits = []
    for line in out.splitlines():
        if any(s in line for s in _CAMPAIGN_SCRIPTS) and "pytest" not in line:
            hits.append(" ".join(line.split()[:6]))
    return hits


def pytest_sessionstart(session):
    busy = _running_campaigns()
    if not busy:
        return
    why = os.environ.get("SUITE_DURING_CAMPAIGN_REASON", "").strip()
    if why:
        print(f"\nWARNING: running the suite during a campaign on purpose: {why}\n"
              f"  {len(busy)} campaign process(es). If a trial fails to "
              f"reproduce later, this is why.\n", flush=True)
        return
    raise pytest.UsageError(
        "a campaign is running; the suite shares src/strategies/current.py with "
        "it and both read and write that path.\n  "
        + "\n  ".join(busy)
        + "\n\nWait for it to finish. If you genuinely need both at once, set "
          "SUITE_DURING_CAMPAIGN_REASON='<why>' -- it is printed and warns "
          "rather than refusing, so the reason is in the log beside whatever "
          "it breaks.")
