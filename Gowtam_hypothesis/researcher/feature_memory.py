"""Shared champion feature memory for the hypothesis research loop.

Persists empirical feature, indicator, parameter, and rule patterns from
completed campaigns so subsequent hypotheses within the same trading question
or theme build upon observed edges rather than starting tabula rasa.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .schemas import HypothesisRecord
    from ..campaign_loop import CampaignOutcome

DEFAULT_MEMORY_PATH = pathlib.Path(__file__).resolve().parents[1] / "output" / "feature_memory.json"


@dataclass
class FeatureEntry:
    hypothesis_id: str
    campaign_id: str
    question: str
    theme: str
    mechanism: str
    observed_best: float
    gate_accept: bool
    indicators: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    entry_rule: str = ""
    exit_rule: str = ""
    timestamp: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class FeatureMemory:
    """Manages empirical feature and champion pattern memory."""

    def __init__(self, path: pathlib.Path | None = None) -> None:
        self.path = path or DEFAULT_MEMORY_PATH
        self.entries: list[FeatureEntry] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            for item in data.get("entries", []):
                self.entries.append(
                    FeatureEntry(
                        hypothesis_id=item["hypothesis_id"],
                        campaign_id=item["campaign_id"],
                        question=item.get("question", ""),
                        theme=item.get("theme", ""),
                        mechanism=item.get("mechanism", ""),
                        observed_best=float(item.get("observed_best", 0.0)),
                        gate_accept=bool(item.get("gate_accept", False)),
                        indicators=item.get("indicators", []),
                        parameters=item.get("parameters", {}),
                        entry_rule=item.get("entry_rule", ""),
                        exit_rule=item.get("exit_rule", ""),
                        timestamp=item.get("timestamp", ""),
                    )
                )
        except Exception:
            # Safe fallback if file corrupted
            self.entries = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "count": len(self.entries),
            "entries": [e.as_dict() for e in self.entries],
        }
        self.path.write_text(json.dumps(payload, indent=2))

    def record_outcome(
        self,
        rec: "HypothesisRecord",
        outcome: "CampaignOutcome",
        question: str = "",
        theme: str = "",
        campaign_dir: pathlib.Path | None = None,
    ) -> FeatureEntry | None:
        """Record champion features if the outcome demonstrated positive score."""
        score = outcome.observed_best
        if score is None or score <= 0.0:
            # We only store positive or gate-accepted signals in champion memory
            return None

        # Extract indicators from specification
        spec = getattr(rec, "specification", None)
        indicators: list[str] = []
        parameters: dict[str, Any] = {}
        entry_rule = ""
        exit_rule = ""

        if spec:
            fields_req = getattr(spec, "data_fields_required", ())
            indicators.extend(list(fields_req))

            sig_def = getattr(spec, "signal_def", "")
            for keyword in ("vwap", "atr", "rsi", "macd", "ema", "sma", "volume", "spread", "delta"):
                if keyword in sig_def.lower() and keyword not in indicators:
                    indicators.append(keyword)

            for p in getattr(spec, "parameters", ()):
                parameters[p.name] = {"min": p.min_val, "max": p.max_val, "step": p.step}

            entry_rule = getattr(spec, "entry_rule", "")
            exit_rule = getattr(spec, "exit_rule", "")

        entry = FeatureEntry(
            hypothesis_id=rec.id,
            campaign_id=outcome.campaign_id,
            question=question,
            theme=theme,
            mechanism=rec.mechanism,
            observed_best=round(score, 4),
            gate_accept=outcome.gate_accept,
            indicators=sorted(set(indicators)),
            parameters=parameters,
            entry_rule=entry_rule[:120],
            exit_rule=exit_rule[:120],
        )

        self.entries.append(entry)
        self.save()
        return entry

    def get_feature_context(self, theme: str = "", question: str = "", top_k: int = 3) -> str:
        """Produce a formatted context string of previous successful patterns."""
        if not self.entries:
            return ""

        # Score relevance: gate_accept gets highest priority, then score, matching theme
        def relevance(e: FeatureEntry) -> tuple[int, int, float]:
            theme_match = 1 if theme and theme.lower() in e.theme.lower() else 0
            accept_match = 1 if e.gate_accept else 0
            return (accept_match, theme_match, e.observed_best)

        sorted_entries = sorted(self.entries, key=relevance, reverse=True)
        top = sorted_entries[:top_k]

        lines = ["Previous high-performing candidate feature patterns for this domain:"]
        for e in top:
            status = "GATE ACCEPTED" if e.gate_accept else f"Sharpe +{e.observed_best:.4f}"
            inds = ", ".join(e.indicators) if e.indicators else "standard price"
            lines.append(
                f"- Mechanism: {e.mechanism} [{status}]\n"
                f"  Indicators used: {inds}\n"
                f"  Entry condition pattern: {e.entry_rule}"
            )

        return "\n".join(lines)
