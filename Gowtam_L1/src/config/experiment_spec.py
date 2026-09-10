"""The fixed experiment contract every future candidate must satisfy.

``ExperimentSpec`` is the single immutable description of "run strategy X over
data Y, exchanged under assumptions Z". It is frozen once defined: later stages
(the agentic loop, debate gate, committee) all build against this exact shape.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

import pandas as pd


@dataclass
class ExperimentSpec:
    asset_ids: list[str]
    start: str
    end: str
    train_end: str
    val_end: str
    strategy_module_path: str
    fee_bps: float = 0.0
    slippage_bps: float = 0.0
    output_dir: str = "output"
    data_source: str = "placeholder.csv"
    data_frequency: str = "1d"

    def validate(self) -> None:
        """Raise a readable error for each invalid field; do not short-circuit."""
        errors: list[str] = []

        if not self.asset_ids:
            errors.append("asset_ids must be non-empty")
        if not self.strategy_module_path:
            errors.append("strategy_module_path must be non-empty")
        if not self.output_dir:
            errors.append("output_dir must be non-empty")

        for name in ("start", "end", "train_end", "val_end"):
            v = getattr(self, name)
            if not v:
                errors.append(f"{name} must be non-empty")

        if not errors:
            start = pd_dt(self.start)
            train_end = pd_dt(self.train_end)
            val_end = pd_dt(self.val_end)
            end = pd_dt(self.end)
            if not (start < train_end):
                errors.append("start must be strictly before train_end")
            if not (train_end < val_end):
                errors.append("train_end must be strictly before val_end")
            if not (val_end < end):
                errors.append("val_end must be strictly before end")

        if errors:
            msg = "; ".join(errors)
            raise ValueError(f"ExperimentSpec validation failed: {msg}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ExperimentSpec":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def pd_dt(value: str):
    return pd.Timestamp(value, tz="UTC")
