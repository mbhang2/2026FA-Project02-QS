"""The corpus registry: every paper we have seen, and what became of it.

`config/retrievals/<name>.json` freezes one answer to one brief. The corpus
pipeline (`scripts/research_pipeline.py`) works the other way round: it walks
the arXiv feed, sees papers nobody asked for, and has to remember what it
already decided about a paper. This module is that memory, and it is a different
shape from a retrieval on purpose:

    runs/corpus/papers/<base-id>.json    one immutable row per paper, by `register`
    runs/corpus/corpus-<year>.jsonl      one append-only JSON line per event

## One row per paper, decided once

`register` writes the metadata row the first time the feed shows a paper and
never writes it again -- a paper re-encountered must not overwrite the row that
answered it first, even if the feed's abstract has moved on. The event log is
one-shot per decision: `screen`, `rerank`, `distil`, `register` and `outcome`
may each appear once for a paper, and a second one raises `RegistryConflict`.
`state` merges the row and the events with first-wins semantics, so a decision
is the first one made, forever.

## Deliberately not a fingerprinted artifact

The corpus is an *input*: it decides which question someone asks. The question
that actually runs is frozen as the committed hypothesis, with its digest in the
campaign record. One frozen thing per campaign is enough, so the registry -- a
moving ledger of decisions no trial ever reads -- is deliberately NOT in
`ARTIFACT_GLOBS`, and that decision is the reason the pipeline doc carries
rather than a gap in it. This module enforces the shape; the v2 plan says why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .._roots import repo_root

CORPUS_ROOT = repo_root() / "runs" / "corpus"

# Events that may occur once per paper. A decision is made once; re-runs and
# re-screens produce new papers or new decisions by hand, not second events.
# `fetch` is not here because a fetch IS the paper row written by `register`,
# which no-ops on a duplicate rather than appending.
ONE_SHOT_KINDS = ("screen", "rerank", "distil", "register", "outcome")
KINDS = ("fetch",) + ONE_SHOT_KINDS


def key_of(arxiv_id: str) -> str:
    """The registry's identity for a paper: the base id, version stripped.

    `2401.12345v2` and `2401.12345v1` are the same paper; the first one the
    corpus saw is the one the row is keyed on.
    """
    head, sep, version = arxiv_id.strip().rpartition("v")
    if head and sep and version.isdigit():
        return head
    return arxiv_id.strip()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegistryError(RuntimeError):
    """The corpus registry is unusable, or was asked for something undefined."""


class RegistryConflict(RegistryError):
    """A one-shot decision already exists for this paper."""


@dataclass
class Registry:
    """The corpus under one root. Read-mostly plus one append seam.

    `year` chooses which year's log new events are appended to, but `events`,
    `state` and `pending` always read every year's log: a decision is a
    decision regardless of which calendar year it was made in.
    """

    root: Path = CORPUS_ROOT
    year: int | None = None

    def __post_init__(self) -> None:
        if self.year is None:
            self.year = _utcnow().year

    # ------------------------------------------------------------------ #
    # the paper rows
    # ------------------------------------------------------------------ #

    def paper_path(self, key: str) -> Path:
        return self.root / "papers" / f"{key}.json"

    def is_registered(self, arxiv_id: str) -> bool:
        return self.paper_path(key_of(arxiv_id)).exists()

    def register(self, paper, query: str) -> tuple[bool, dict]:
        """Write the paper's row, or leave the existing row byte-for-byte alone.

        Returns `(created, record)`. A duplicate never rewrites the row on
        disk -- even a tampered row is left exactly as found, because overwrite
        is the failure mode this method exists to prevent. A paper that turns
        up in a later feed run re-registers as a no-op; the row that answered
        first is the row that stays.
        """
        key = key_of(paper.arxiv_id)
        path = self.paper_path(key)
        if path.exists():
            try:
                return False, json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                raise RegistryError(
                    f"{path} is not readable JSON. The registry never "
                    "overwrites a paper row, so a corrupt row must be fixed by "
                    "hand or the paper re-registered under a clean root.") from exc
        record = {
            "archive": "corpus",
            "key": key,
            "arxiv_id": paper.arxiv_id,
            "title": paper.title,
            "authors": list(paper.authors),
            "published": paper.published,
            "abstract": paper.abstract,
            "categories": list(paper.categories),
            "query": query,
            "date_pulled": _utcnow().isoformat(timespec="seconds"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        return True, record

    def _load_paper(self, key: str) -> dict | None:
        path = self.paper_path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    # ------------------------------------------------------------------ #
    # the event log
    # ------------------------------------------------------------------ #

    def log_path(self) -> Path:
        return self.root / f"corpus-{self.year}.jsonl"

    def _year_logs(self) -> list[Path]:
        return sorted(self.root.glob("corpus-*.jsonl"))

    def append(self, *, kind: str, arxiv_id: str, date: str | None = None,
               **fields) -> dict:
        """Append one event line. One-shot kinds raise on a repeat."""
        if kind not in KINDS:
            raise RegistryError(f"kind {kind!r} is not one of {KINDS}")
        key = key_of(arxiv_id)
        if not key:
            raise RegistryError("append needs a non-empty arxiv_id")
        if kind in ONE_SHOT_KINDS and self._events_for(key, kinds={kind}):
            raise RegistryConflict(
                f"{key} already has a {kind} decision; a decision is made "
                "once. Nothing is overwritten.")
        record = {"kind": kind, "arxiv_id": key,
                  "date": date or _utcnow().isoformat(timespec="seconds"),
                  **fields}
        self.log_path().parent.mkdir(parents=True, exist_ok=True)
        with self.log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def _iterate_events(self) -> list[tuple[str, dict]]:
        """Every event line across every year's log, as (key, record)."""
        out: list[tuple[str, dict]] = []
        for path in self._year_logs():
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RegistryError(
                        f"{path}:{lineno} is not a JSON event line. The log is "
                        "append-only; a corrupted line is a tamper, not a "
                        "skip.") from exc
                key = str(record.get("arxiv_id") or "")
                if not key:
                    raise RegistryError(f"{path}:{lineno} has no arxiv_id")
                out.append((key, record))
        return out

    def _events_for(self, key: str, kinds: set[str] | None = None) -> list[dict]:
        return [rec for k, rec in self._iterate_events()
                if k == key and (kinds is None or rec["kind"] in kinds)]

    def events(self, arxiv_id: str | None = None) -> list[dict]:
        if arxiv_id is None:
            return [rec for _, rec in self._iterate_events()]
        return self._events_for(key_of(arxiv_id))

    def keys(self) -> list[str]:
        keys: set[str] = set()
        for path in self.root.glob("papers/*.json"):
            keys.add(path.stem)
        for key, _ in self._iterate_events():
            keys.add(key)
        return sorted(keys)

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #

    def state(self, arxiv_id: str) -> dict:
        """The paper row plus every decision, merged first-wins."""
        key = key_of(arxiv_id)
        st = self._load_paper(key) or {"key": key}
        for rec in self._events_for(key):
            for k, v in rec.items():
                if k in ("kind", "arxiv_id", "date"):
                    continue
                if k == "campaign_id":
                    ids = st.setdefault("campaign_ids", [])
                    if v not in ids:
                        ids.append(v)
                else:
                    st.setdefault(k, v)
        return st

    def states(self) -> list[dict]:
        return [self.state(key) for key in self.keys()]

    def pending(self, stage: str) -> list[str]:
        """Base ids ready for a stage, in key order."""
        if stage not in ("screen", "rerank", "distil", "register"):
            raise RegistryError(f"no pending set named {stage!r}")
        out: list[str] = []
        for key in self.keys():
            st = self.state(key)
            testable = st.get("feasibility") in ("direct", "adaptable")
            if stage == "screen":
                if "feasibility" not in st:
                    out.append(key)
            elif stage == "rerank":
                if testable and "demoted" not in st:
                    out.append(key)
            elif stage == "distil":
                if (testable and st.get("demoted") is not True
                        and "hypothesis_path" not in st):
                    out.append(key)
            elif stage == "register":
                if "hypothesis_path" in st and "campaign_id" not in st:
                    out.append(key)
        return out