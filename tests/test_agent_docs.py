"""The two documents the research agent reads have to agree with each other.

`docs/agent/data.md` is generated from the manifest and the dataclasses and says
what the data *is*. `docs/agent/strategy-api.md` is written and says what you can
*do* with it. Together they are the agent's entire context, so a disagreement
between them is not a documentation problem -- it is a strategy written against
a field that does not exist, discovered at trial 200.

These checks are deliberately narrow. They cannot tell you the prose is right;
they can tell you the two documents have stopped naming the same things.
"""

from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.types import Bar, TickEvent  # noqa: E402

DATA_DOC = REPO_ROOT / "docs" / "agent" / "data.md"
API_DOC = REPO_ROOT / "docs" / "agent" / "strategy-api.md"


def test_both_documents_exist_and_are_not_stubs():
    for doc in (DATA_DOC, API_DOC):
        assert doc.exists(), doc
        assert len(doc.read_text().splitlines()) > 80, f"{doc} looks like a stub"


def test_the_written_doc_defers_to_the_generated_one():
    """One authority per fact. The generated document is regenerated when the
    data changes; the written one cannot be, so it must point rather than copy."""
    text = API_DOC.read_text()
    assert "docs/agent/data.md" in text, (
        "strategy-api.md no longer points at data.md, which is the only thing "
        "keeping field semantics in one place"
    )


def test_field_names_named_in_the_written_doc_are_real():
    """strategy-api.md mentions a handful of fields by name in prose. Each has to
    be a field that actually exists on the type the agent receives."""
    text = API_DOC.read_text()
    bar_fields = {f.name for f in fields(Bar)}
    tick_fields = {f.name for f in fields(TickEvent)}
    for name in ("open_ts", "close_ts", "tick_count"):
        assert name in bar_fields, f"{name} is not a Bar field any more"
        assert name in text, f"strategy-api.md stopped mentioning {name}"
    assert "side" in tick_fields
    assert "`side`" in text


def test_both_documents_agree_that_unknown_aggressor_is_none():
    """The single easiest thing to get silently wrong, and the one place the two
    documents must not drift: `N` from the feed arrives as `None`, is real, and
    must not be folded into a side."""
    for doc in (DATA_DOC, API_DOC):
        text = doc.read_text()
        assert "`None`" in text, f"{doc.name} no longer says unknown side is None"
        assert "unknown" in text.lower(), f"{doc.name} no longer names the third value"


def test_the_generated_doc_lists_every_field_of_what_the_agent_receives():
    """If a field is added to Bar or TickEvent, the generator fails rather than
    producing an incomplete table. This asserts the outcome of that, so the two
    cannot silently diverge if the generator's guard is ever weakened."""
    text = DATA_DOC.read_text()
    for cls in (Bar, TickEvent):
        for f in fields(cls):
            assert f"`{f.name}`" in text, (
                f"{cls.__name__}.{f.name} is not in docs/agent/data.md; "
                "run scripts/gen_agent_data.py"
            )


def test_the_generated_data_reference_is_current():
    """`gen_agent_data.py --check` was a CI step and not a test, so a stale
    generated document failed the build but not `pytest` -- and pytest is where
    people actually look. Same class of gap as everything else Task 17 closed: a
    guard that does not guard where the work happens.

    The context API reference has been checked this way since Task 5; this is the
    other generator catching up.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "scripts/gen_agent_data.py", "--check"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        result.stderr.strip()
        or "docs/agent/data.md is stale; run scripts/gen_agent_data.py"
    )


# --------------------------------------------------------------------------- #
# the document the proposer is shown must not promise what the engine refuses
# --------------------------------------------------------------------------- #
API_DOC = REPO_ROOT / "docs" / "agent" / "strategy-api.md"


def test_every_order_keyword_the_doc_lists_can_actually_be_sent():
    """**The most expensive bug shape this repo has.** `strategy-api.md` is
    pasted verbatim into every proposal prompt, so a keyword it documents and
    the transport refuses is an instruction to crash. Measured 2026-10-14:
    `target` was documented as an absolute bracket price ("usually what you
    mean") and missing from the protocol allowlist, and `expire_ts` was
    documented as required for GTD while the allowlist carried a nonexistent
    `expire_ns` -- 16 crashed trials between them, each charged to N.
    """
    import re

    from src.core.engine.remote.protocol import ACTIONS

    text = API_DOC.read_text()
    # the order-keyword table: rows of the form `| `name` | meaning |`
    table = text.split("### Orders", 1)[1].split("### ", 1)[0]
    documented = set()
    for line in table.splitlines():
        if not line.startswith("|"):
            continue
        first_cell = line.split("|")[1]           # the keyword column
        for name in re.findall(r"`([a-z_]+)`", first_cell):
            documented.add(name)
    assert {"stop", "target", "stop_offset", "target_offset"} <= documented, (
        "the bracket keywords vanished from the doc table; this test is blind")
    sendable = ACTIONS["buy"] | {"size"}
    promised_but_refused = sorted(documented - sendable)
    assert not promised_but_refused, (
        f"strategy-api.md documents order keywords the protocol refuses: "
        f"{promised_but_refused}. The proposer is being told to crash.")


def test_the_doc_does_not_tell_the_agent_to_check_len_and_index():
    """`len()` on a `HistoryView` is the true number of closed bars, while the
    engine pushes only the last N. "Check `len()`" followed by indexing or
    iterating is therefore a crash, and it was the single most common one --
    25 of 162 crashed trials. The doc used to recommend exactly that."""
    text = API_DOC.read_text()
    assert "check `len()` rather than catching an exception" not in text, (
        "the doc is back to recommending the idiom that caused 25 crashes")
    assert "`len()` is not how much you can read" in text
    assert "last(200)" in text or "last(k)" in text, (
        "the doc no longer shows the bounded idiom")


def test_the_doc_and_the_engine_agree_that_last_is_the_safe_call():
    """`last(k)` must keep returning what exists rather than raising, or the
    advice above becomes wrong in the other direction."""
    from src.core.engine.view import HistoryView

    view = HistoryView([1, 2, 3])
    assert view.last(10) == (1, 2, 3)
    assert view.last(2) == (2, 3)
    assert view.last(0) == ()
