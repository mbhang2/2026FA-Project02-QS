"""Tier 3's own tests, brought into the root suite by Task 44.

Eight of them need demo experiment fixtures under `research/examples/` that
**exist in no branch of this repository** — verified against a throwaway worktree
at the audit agent's own root commit, which fails identically. Nothing in any
`.gitignore` excludes them; they were simply never committed.

They are **skipped with a reason, not deleted and not relaxed.** A test that
cannot pass says what it needs; a test that was removed says nothing, and a test
edited until it passes says something false. The moment the fixtures land these
run unchanged.

The four that do not need fixtures are not skipped, because skipping them would
be losing four real tests to tidy a count.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "research" / "examples"

# Named explicitly rather than inferred from the failure, so the list is a claim
# somebody can check rather than a net that quietly widens.
NEEDS_DEMO_FIXTURES = {
    "test_buy_hold_audit_passes",
    "test_cheat_audit_fails",
    "test_metric_mismatch_fails_reproduce",
    "test_strategy_alias_from_experiment_json",
    "test_tsmom_ticket_pause",
    "test_cheat_audit_kills_without_store",
    "test_list_includes_demo_ids",
    "test_list_includes_nvda_demo",
}

REASON = (
    "needs demo experiment fixtures under research/examples/ "
    "(01-lookahead-cheat, 01-realistic, nvda-1m-realistic). They exist in no "
    "branch and nothing gitignores them -- they were never committed. Ask the "
    "author; do not invent files whose expected verdicts this test asserts."
)


def pytest_collection_modifyitems(config, items):
    if FIXTURES.is_dir():
        return
    skip = pytest.mark.skip(reason=REASON)
    for item in items:
        if item.name.split("[")[0] in NEEDS_DEMO_FIXTURES:
            item.add_marker(skip)
