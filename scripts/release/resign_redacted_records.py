"""Re-sign committed run records after the public-release redaction.

Why this exists
---------------
The team's LLM proxy endpoint was recorded inside every campaign record (the
`endpoint` field of the proposer's provenance) and in a few run artefacts. The
endpoint is private, so for the public repository it was replaced everywhere --
records, code, tests and history -- with the placeholder `llm-proxy.invalid`
(`.invalid` is reserved by RFC 2606 and can never resolve). The real endpoint is
supplied at run time through the `LITELLM_BASE_URL` environment variable.

Campaign records are tamper-evident: each stores a SHA-256 of its own contents,
and the files that cite a record carry that digest forward. Changing one string
inside a record therefore makes it read as tampered, which is the failure this
project exists to catch. This script is the disclosed, reproducible repair:

  1. every `campaign.json` is re-hashed with `CampaignRecord.digest()`;
  2. each changed digest (full and 12-character form) is substituted in the
     files that cite it;
  3. every `tier2-summary.json` is re-hashed with `campaign_summary.digest_of`,
     and its changed digest is substituted in turn.

Nothing else in a record is altered. `docs/PUBLIC_RELEASE.md` lists exactly what
the redaction touched. Run from the repository root:

    python scripts/release/resign_redacted_records.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.campaign_summary import digest_of  # noqa: E402
from src.core.loop.campaign import CampaignRecord  # noqa: E402

RUNS = ROOT / "runs"
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".py", ".txt", ".yaml"}


def _substitute(mapping: dict[str, str]) -> int:
    """Replace each old digest (and its 12-char prefix) everywhere under runs/."""
    if not mapping:
        return 0
    pairs = []
    for old, new in mapping.items():
        pairs.append((old, new))
        pairs.append((old[:12], new[:12]))
    touched = 0
    for path in RUNS.rglob("*"):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        text = path.read_text()
        updated = text
        for old, new in pairs:
            updated = updated.replace(old, new)
        if updated != text:
            path.write_text(updated)
            touched += 1
    return touched


def resign_campaigns() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path in sorted(RUNS.rglob("campaign.json")):
        body = json.loads(path.read_text())
        new = CampaignRecord(**body["record"]).digest()
        if new != body["digest"]:
            mapping[body["digest"]] = new
            body["digest"] = new
            path.write_text(json.dumps(body, indent=2, sort_keys=True, default=str) + "\n")
    return mapping


def resign_tier2() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path in sorted(RUNS.rglob("tier2-summary.json")):
        summary = json.loads(path.read_text())
        new = digest_of({k: v for k, v in summary.items()
                         if k not in ("written_at", "digest")})
        if new != summary.get("digest"):
            mapping[summary["digest"]] = new
            summary["digest"] = new
            path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")
    return mapping


def main() -> int:
    campaigns = resign_campaigns()
    files = _substitute(campaigns)
    print(f"campaign records re-signed: {len(campaigns)}; citing files updated: {files}")
    tier2 = resign_tier2()
    files = _substitute(tier2)
    print(f"tier-2 summaries re-signed: {len(tier2)}; citing files updated: {files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
