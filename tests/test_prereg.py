"""Is amendment N in force? The document is the switch, so this is the parse.

The load-bearing case is a substring bug that guarded the single irrecoverable
holdout look. `holdout.py` asked "'IN FORCE' present and 'NOT IN FORCE' absent",
and **"REGISTERED, NOT YET IN FORCE"** satisfies both halves -- it contains
"IN FORCE" and does not contain "NOT IN FORCE". Amendments 4 and 7 are written
that way. The check protecting 73 sessions would have unlocked on a status line
saying, in English, the opposite.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "scripts" / "prereg.py").read_text()
FLAT = " ".join(SRC.split())


def _mod():
    spec = importlib.util.spec_from_file_location(
        "prereg", REPO_ROOT / "scripts" / "prereg.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doc(status: str | None, n: int = 9) -> str:
    head = f"## Amendment {n} — 2026-09-30 — a rule\n\n"
    body = "the rule itself, at length.\n"
    return head + (f"**Status: {status}.** and then prose\n\n" if status else "") + body


# --------------------------------------------------------------------------- #
# the bug this module exists to kill
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [
    "REGISTERED, NOT YET IN FORCE",
    "DRAFTED, NOT YET IN FORCE",
    "DRAFTED, NOT IN FORCE",
    "REGISTERED, NOT YET IMPLEMENTED",
    "SUPERSEDED, was IN FORCE until 2026-09-01",
])
def test_a_status_that_is_not_a_signature_is_not_in_force(status):
    """Every one of these contains the words "IN FORCE" somewhere. None of them
    is a signature, and the old substring test called two of them signed."""
    assert _mod().in_force(9, _doc(status)) is False, status


@pytest.mark.parametrize("status", [
    "IN FORCE from 2026-09-30, signed off by Michael",
    "IN FORCE from commit `012c26b`, 2026-09-20",
    "in force from today",
])
def test_a_status_that_begins_with_the_signature_is_in_force(status):
    assert _mod().in_force(9, _doc(status)) is True, status


def test_an_amendment_with_no_status_line_is_unconditional():
    """Amendments 1 and 2 are dated statements of fact with no condition
    attached. Reporting them as "not in force" would be wrong, and the honest
    encoding is that the absence of a condition IS the condition."""
    mod = _mod()
    assert mod.in_force(9, _doc(None)) is True
    assert "unconditional" in mod.status(9, _doc(None))


def test_a_missing_amendment_is_never_in_force():
    """Every caller is gating something irreversible, so an amendment that
    cannot be read is not one anybody signed."""
    mod = _mod()
    assert mod.in_force(99, _doc("IN FORCE from 2026-09-30")) is False
    assert "no such section" in mod.status(99, _doc("IN FORCE"))


def test_a_section_about_an_amendment_is_not_that_amendment():
    """`## Amendment 6's disclosure gap` is a section ABOUT Amendment 6. Matching
    it as Amendment 6's own head would read a post-mortem's first paragraph as a
    status line."""
    text = ("## Amendment 6's disclosure gap — found 2026-09-25\n\n"
            "**Status: IN FORCE from 2026-09-25.**\n\nthe post-mortem\n\n"
            "## Amendment 6 — 2026-09-24 — the real one\n\n"
            "**Status: REGISTERED, NOT YET IMPLEMENTED.**\n\nthe rule\n")
    mod = _mod()
    assert mod.in_force(6, text) is False
    assert "NOT YET IMPLEMENTED" in mod.status(6, text)


def test_the_status_stops_at_the_closing_bold():
    """The prose continues on the same line, so a naive strip of trailing
    asterisks returns the first sentence of the body as part of the status."""
    mod = _mod()
    line = mod.status(9, _doc("IN FORCE from 2026-09-30"))
    assert line == "Amendment 9: IN FORCE from 2026-09-30.", line
    assert "and then prose" not in line


# --------------------------------------------------------------------------- #
# the real document, as a canary
# --------------------------------------------------------------------------- #
def test_the_real_documents_current_statuses():
    """A snapshot, so an accidental edit to a status line fails here rather than
    changing what a gate does. Update it deliberately when an amendment is
    signed, which is the point at which somebody should be reading this list."""
    mod = _mod()
    expected = {1: True, 2: True, 3: True, 4: False, 5: False,
                6: False, 7: False, 8: True, 9: True, 10: True, 11: True}
    actual = {n: ok for n, ok, _ in mod.all_amendments()}
    assert actual == expected, actual


def test_amendment_9_is_in_force_and_the_holdout_is_still_shut():
    """Amendment 9 was signed on 2026-09-30. It is stated separately from the
    snapshot because it guards the single look, and a reader skimming ten
    booleans will not see it.

    Signing it removes ONE refusal. The look stays shut on the others, and that
    is asserted here so nobody reads "the pass rule is in force" as "the holdout
    is open"."""
    import importlib.util, json, pathlib as pl
    assert _mod().in_force(9) is True
    spec = importlib.util.spec_from_file_location(
        "holdout", REPO_ROOT / "scripts" / "holdout.py")
    h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
    assert not any("Amendment 9" in r for r in h.blocked(rehearse=False))
    assert h.blocked(rehearse=False), (
        "the holdout must still be blocked by something -- if this fails, the "
        "single look is one command away and somebody should know that")


def test_the_document_is_the_switch_and_says_so():
    assert "The document is the switch" in FLAT
    assert "boolean in code" in FLAT or "flag" in FLAT
