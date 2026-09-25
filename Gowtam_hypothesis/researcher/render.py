"""Flatten a ``HypothesisRecord`` into the markdown ``config/hypotheses/*.md``.

The rendered text is the researcher's *only* hand-off to the MVP rail. The rail
applies two filters to it that this renderer must not trip, and the two are not
the same risk:

1. ``src.core.loop.hypothesis.lint`` -- the gate-vocabulary lint. The renderer's
   own boilerplate must be clean of banned terms ("accept", "reject",
   "significant", "hold out", "N trials", "correction", ...). The *field
   content* is the researcher's responsibility, but the boilerplate here is
   fixed, so it is the renderer's job to be clean by construction.

2. The leak edge -- a rendered file must never smuggle a realized eval-window
   outcome into the prompt. That is the *researcher's* specific failure mode and
   is caught by ``researcher.leak_lint`` layered on top of the rail's lint, not
   by this module.

The renderer does not call the rail's lint itself: the driver does, because the
rail lint is the final authority and there must be no code path that writes to
``config/hypotheses/`` without passing it. Rendering is pure and importable
without the MVP repository on the path, so the schema can be unit-tested in
isolation.
"""

from __future__ import annotations

from .schemas import HypothesisRecord


def render(rec: HypothesisRecord) -> str:
    """Return clean markdown. Deterministic given the record.

    The section headings are deliberately plain. They name the parts of a
    hypothesis a proposer needs (mechanism, the claim, how to make it concrete,
    what data, where it came from) without drifting into words the lint owns.
    """
    data_lines = [f"- interval: {rec.data_requires.interval}",
                  f"- needs ticks: {'yes' if rec.data_requires.needs_ticks else 'no'}",
                  f"- needs spreads: {'yes' if rec.data_requires.needs_spreads else 'no'}"]
    if rec.data_requires.other.strip():
        data_lines.append(f"- other: {rec.data_requires.other.strip()}")

    source_lines = [
        f"- [{sc.source_title or sc.source_url}]({sc.source_url}): {sc.claim.strip()}"
        for sc in rec.sources
    ]

    parts = [
        f"# {rec.id}",
        "",
        "Written by the researcher; the commit date is the pre-registration date.",
        "",
        "## The mechanism, and why it might work",
        "",
        rec.mechanism.strip(),
        "",
        "## The claim this makes testable",
        "",
        rec.falsifiable.strip(),
        "",
        "## How to make it concrete",
        "",
        rec.operationalize.strip(),
        "",
        "## Data it needs",
        "",
        *data_lines,
        "",
        "## Where it came from",
        "",
        *source_lines,
        "",
        "## Why it is worth a trial",
        "",
        (rec.confidence_prior.strip() or "(no prior given)"),
        "",
    ]
    return "\n".join(parts)
