"""Stage 0 of paper retrieval: the parser and the registry's idempotence.

No network. `search()` is a thin loop over `_get` and arXiv's own paging; what is
worth pinning is that a feed parses into the shape the registry expects, and that
re-running a fetch cannot erase a decision somebody made about a paper.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "fetch_papers", REPO_ROOT / "scripts" / "fetch_papers.py")
fp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fp)


FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2609.12345v2</id>
    <title>Order   flow
      imbalance and short-horizon returns</title>
    <summary>We study   whether signed
      volume predicts returns.</summary>
    <published>2026-09-01T00:00:00Z</published>
    <updated>2026-09-02T00:00:00Z</updated>
    <author><name>A. Researcher</name></author>
    <author><name>B. Coauthor</name></author>
  </entry>
</feed>"""


def test_a_feed_parses_into_the_shape_the_registry_expects():
    got = fp.parse_feed(FEED)
    assert len(got) == 1
    p = got[0]
    assert p["id"] == "2609.12345v2", "the bare arXiv id, not the abs URL"
    assert p["source"] == "arxiv"
    # Atom wraps titles and abstracts over lines; a registry full of embedded
    # newlines makes every downstream prompt and diff worse.
    assert p["title"] == "Order flow imbalance and short-horizon returns"
    assert "\n" not in p["abstract"]
    assert p["authors"] == ["A. Researcher", "B. Coauthor"]


def test_a_malformed_entry_is_skipped_rather_than_fatal():
    """One bad entry in a page of a hundred should not lose the other 99."""
    bad = FEED.replace(b"<id>http://arxiv.org/abs/2609.12345v2</id>", b"")
    assert fp.parse_feed(bad) == []
    two = FEED.replace(b"</feed>", FEED.split(b"<feed", 1)[1]
                       .split(b">", 1)[1].replace(b"</feed>", b"") + b"</feed>")
    assert len(fp.parse_feed(two)) >= 1


def test_registering_a_known_paper_changes_nothing():
    """The guard that matters. `status`, `feasibility` and `outcome` are
    decisions; re-running a fetch is not a reason to forget them."""
    reg = {"schema": "paper-registry/1", "papers": {}}
    paper = fp.parse_feed(FEED)[0]
    assert fp.register(reg, paper, "q") is True

    row = reg["papers"][paper["id"]]
    row["feasibility"] = "adaptable"
    row["status"] = "campaign_run"
    row["outcome"] = "REJECT, p=0.98"
    row["campaign_ids"] = ["nq-foo-01"]
    snapshot = json.dumps(row, sort_keys=True)

    assert fp.register(reg, paper, "different query") is False
    assert json.dumps(reg["papers"][paper["id"]], sort_keys=True) == snapshot, (
        "a second fetch overwrote a decision already made about this paper")
    assert len(reg["papers"]) == 1


def test_a_new_row_starts_undecided():
    reg = {"schema": "paper-registry/1", "papers": {}}
    fp.register(reg, fp.parse_feed(FEED)[0], "q")
    row = reg["papers"]["2609.12345v2"]
    assert row["status"] == "unread"
    assert row["feasibility"] is None and row["relevance"] is None
    assert row["campaign_ids"] == [] and row["outcome"] is None


def test_tls_verification_is_on():
    """The macOS install has no system trust store, and the tempting fix is
    `ssl._create_unverified_context`. A retrieval pipeline that trusts whatever
    answers is worse than one that does not run."""
    src = (REPO_ROOT / "scripts" / "fetch_papers.py").read_text()
    assert "_create_unverified_context" not in src
    assert "CERT_NONE" not in src
    ctx = fp._ssl_context()
    import ssl
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_portfolio_management_is_not_a_default_category():
    """q-fin.PM is almost entirely cross-sectional, which the design calls
    infeasible on one instrument. Fetching it would fill the registry with
    papers the screen must then reject one at a time."""
    assert "q-fin.PM" not in fp.DEFAULT_CATEGORIES
    assert "q-fin.TR" in fp.DEFAULT_CATEGORIES


def test_the_rate_limit_is_what_arxiv_asks_for():
    assert fp.RATE_LIMIT_S >= 3.0
    assert "2026FA-Project02" in fp.USER_AGENT
