"""Cross-campaign constraints, compiled into a frozen prompt block.

The store is `docs/knowledge/log.jsonl`. Humans and scripts read it. The
proposing agent never does. `compile_view` is deterministic: closed families
and harness rules for the campaign's dataset, nothing else. The bytes go in
`runs/campaigns/<id>/knowledge.md` at register and are hashed into the campaign
record. Mid-run edits to the store cannot change an in-flight prompt.

**What this is not.** It is not a memory of scores, parameters or windows.
Those stay in the store, next to `how_it_died`. An agent shown last campaign's
winner searches the neighborhood of a training-set draw; the within-campaign
history whitelist already prices trial-to-trial adaptation.

Empty `block(None)` keeps the `{knowledge}` slot additive the way
`hypothesis.block(None)` does: substituting "" leaves the template's blank
line, so a knowledge-less campaign still matches Task 45's messages.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .._roots import repo_root
from .hypothesis import findings as gate_findings, lint as gate_lint

REPO_ROOT = repo_root()
STORE_DIR = REPO_ROOT / "docs" / "knowledge"
LOG_PATH = STORE_DIR / "log.jsonl"
CLOSED_MD = STORE_DIR / "closed.md"
OPEN_MD = STORE_DIR / "open.md"

KINDS = frozenset({
    "closed_family", "open_family", "near_miss", "harness_lesson",
    "campaign_index",
})
STATUSES = frozenset({
    "closed", "open", "unconfirmed", "refuted", "harness",
})
MARKET_KINDS = frozenset({"closed_family", "open_family", "near_miss"})

# Extra leaks the gate lint does not cover: parameter pairs, calendar windows,
# and "this scored well". Applied only to text the compiler would emit.
SNAPSHOT_BANNED: tuple[tuple[str, str], ...] = (
    (r"\b\d+\s*/\s*\d+\b", "is a parameter pair (period/period)"),
    (r"\b(?:january|february|march|april|may|june|july|august|september|"
     r"october|november|december)\b.{0,40}\b20\d{2}\b",
     "is a calendar window"),
    (r"\bscored\b", "names a result"),
    (r"\bworked well\b", "names a result"),
    (r"\bscore\s*=", "names a result"),
    (r"\bp\s*=", "is the gate's output"),
)


class KnowledgeRejected(ValueError):
    """The store or the compiled view says something the agent must not be told."""


@dataclass(frozen=True, slots=True)
class KnowledgeView:
    """A compiled, linted snapshot. Frozen at register, like a hypothesis."""

    path: Path | None
    text: str
    digest: str


def snapshot_findings(text: str) -> list[str]:
    """Gate vocabulary plus parameter/window/result leaks. Empty means clean."""
    out = list(gate_findings(text))
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern, why in SNAPSHOT_BANNED:
            for m in re.finditer(pattern, line, flags=re.IGNORECASE):
                out.append(f"line {lineno}: {m.group(0)!r} -- {why}")
    return out


def lint_snapshot(text: str, *, source: str = "<knowledge>") -> None:
    found = snapshot_findings(text)
    if found:
        raise KnowledgeRejected(
            f"{source} would tell the agent about the gate, a closed split, "
            f"a parameter, or a result:\n  " + "\n  ".join(found)
        )


def load_log(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or LOG_PATH
    if not path.is_file():
        raise FileNotFoundError(f"no knowledge log at {path}")
    rows = []
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise KnowledgeRejected(f"{path}:{i} is not JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise KnowledgeRejected(f"{path}:{i} is not an object")
        rows.append(row)
    return rows


def _require(row: dict, *keys: str) -> list[str]:
    return [k for k in keys if not row.get(k)]


def check_row(row: dict[str, Any], *, ids: set[str]) -> list[str]:
    """Schema problems on one row. Empty means the row is well-formed."""
    errors: list[str] = []
    rid = row.get("id")
    if not isinstance(rid, str) or not rid:
        errors.append("missing id")
        rid = "<no-id>"
    kind = row.get("kind")
    if kind not in KINDS:
        errors.append(f"{rid}: kind {kind!r} is not one of {sorted(KINDS)}")
    status = row.get("status")
    if status not in STATUSES:
        errors.append(f"{rid}: status {status!r} is not one of {sorted(STATUSES)}")
    if row.get("visible_to_agent") is not False:
        errors.append(f"{rid}: visible_to_agent must be false on the store; "
                      "the compiler decides what the snapshot contains")
    if kind == "campaign_index":
        errors.extend(f"{rid}: missing {k}" for k in _require(row, "campaign_id"))
    elif kind == "near_miss":
        errors.extend(f"{rid}: missing {k}" for k in
                      _require(row, "claim", "how_it_died", "campaign_id"))
    elif kind == "harness_lesson":
        errors.extend(f"{rid}: missing {k}" for k in _require(row, "claim"))
    elif kind in {"closed_family", "open_family"}:
        errors.extend(f"{rid}: missing {k}" for k in _require(row, "claim"))
    if kind in MARKET_KINDS:
        errors.extend(f"{rid}: missing {k}" for k in _require(row, "dataset", "split"))
    sup = row.get("supersedes")
    if sup is not None and sup not in ids:
        errors.append(f"{rid}: supersedes {sup!r} which is not in the log")
    grade = row.get("evidence_grade")
    if grade is not None and grade not in (0, 1, 2, 3, 4):
        errors.append(f"{rid}: evidence_grade {grade!r} is not 0..4")
    return errors


def check_store(path: Path | None = None) -> list[str]:
    """Every problem in the log and in the human markdown that must stay clean."""
    path = path or LOG_PATH
    rows = load_log(path)
    ids = [row.get("id") for row in rows]
    dupes = {i for i in ids if isinstance(i, str) and ids.count(i) > 1}
    errors: list[str] = []
    if dupes:
        errors.append(f"duplicate ids: {sorted(dupes)}")
    id_set = {i for i in ids if isinstance(i, str)}
    for row in rows:
        errors.extend(check_row(row, ids=id_set))
        kind = row.get("kind")
        if kind in {"closed_family", "open_family"} and row.get("claim"):
            leaked = snapshot_findings(row["claim"])
            errors.extend(f"{row.get('id')}: claim {x}" for x in leaked)
        rule = row.get("rule_for_agent")
        if rule:
            leaked = snapshot_findings(rule)
            errors.extend(f"{row.get('id')}: rule_for_agent {x}" for x in leaked)
    root = path.parent
    for md in (root / "closed.md", root / "open.md"):
        if md.is_file():
            leaked = snapshot_findings(md.read_text())
            errors.extend(f"{md.name}: {x}" for x in leaked)
    return errors


def compile_view(rows: Iterable[dict[str, Any]] | None = None, *,
                 dataset: str) -> str:
    """Deterministic agent snapshot for `dataset`. Empty string if nothing applies.

    Includes `closed_family` rows whose dataset matches, and every
    `harness_lesson` that carries `rule_for_agent`. Drops near-misses,
    campaign indexes, open families, and any row whose emit text fails the lint
    — by raising, not by skipping, so a leak cannot silently vanish.
    """
    rows = list(rows) if rows is not None else load_log()
    closed, harness = [], []
    for row in sorted(rows, key=lambda r: str(r.get("id") or "")):
        kind = row.get("kind")
        if kind == "closed_family" and row.get("dataset") == dataset:
            text = (row.get("claim") or "").strip()
            reopen = (row.get("reopen_if") or "").strip()
            if reopen:
                text = f"{text} Reopen if: {reopen}"
            lint_snapshot(text, source=str(row.get("id")))
            closed.append(text)
        elif kind == "harness_lesson":
            rule = (row.get("rule_for_agent") or "").strip()
            if not rule:
                continue
            lint_snapshot(rule, source=str(row.get("id")))
            harness.append(rule)
    if not closed and not harness:
        return ""
    parts = [
        "### Constraints carried from prior research",
        "",
        "These are bounds on the search, written before the first proposal. "
        "They do not name a result on the data, a parameter, or a window of "
        "the sample.",
    ]
    if closed:
        parts.extend(["", "#### Do not re-explore", ""])
        parts.extend(f"- {c}" for c in closed)
    if harness:
        parts.extend(["", "#### Do not do this", ""])
        parts.extend(f"- {h}" for h in harness)
    text = "\n".join(parts) + "\n"
    lint_snapshot(text, source="<compiled knowledge>")
    gate_lint(text, source="<compiled knowledge>")
    return text


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write_view(path: Path, *, dataset: str,
               rows: Iterable[dict[str, Any]] | None = None) -> KnowledgeView:
    """Compile and write the campaign-local snapshot. Returns the view."""
    text = compile_view(rows, dataset=dataset)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return KnowledgeView(path=path, text=text, digest=digest_text(text))


def load_view(path: Path) -> KnowledgeView:
    """Read a frozen snapshot, lint it, digest it. The only way it reaches an agent."""
    if not path.is_file():
        raise FileNotFoundError(
            f"no knowledge snapshot at {path}. A campaign either compiled one "
            "at register or runs without one -- there is no third option where "
            "the constraints live in an unhashed file.")
    text = path.read_text()
    if text.strip():
        lint_snapshot(text, source=str(path))
        gate_lint(text, source=str(path))
    return KnowledgeView(path=path, text=text, digest=digest_text(text))


def block(view: KnowledgeView | None) -> str:
    """The prompt section, or the empty string.

    Empty reproduces the pre-slot messages byte for byte when concatenated
    with `{hypothesis}` on the same template line.
    """
    if view is None or not view.text.strip():
        return ""
    return "\n" + view.text.strip() + "\n"


def campaign_ids_on_disk(campaigns_root: Path | None = None) -> set[str]:
    root = campaigns_root or (REPO_ROOT / "runs" / "campaigns")
    found: set[str] = set()
    if not root.is_dir():
        return found
    for path in root.rglob("campaign.json"):
        try:
            body = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        cid = (body.get("record") or {}).get("campaign_id")
        if cid:
            found.add(cid)
    return found


def check_campaign_index(rows: Iterable[dict[str, Any]] | None = None,
                         campaigns_root: Path | None = None) -> list[str]:
    """Every `campaign_id` named in the log must exist as a campaign record."""
    rows = list(rows) if rows is not None else load_log()
    known = campaign_ids_on_disk(campaigns_root)
    errors = []
    for row in rows:
        cid = row.get("campaign_id")
        if cid and cid not in known:
            errors.append(f"{row.get('id')}: campaign_id {cid!r} is not on disk")
    return errors
