#!/usr/bin/env python3
"""Re-run a ledger row from the strategy source stored beside it.

    python scripts/replay.py runs/ledger.jsonl 47
    python scripts/replay.py runs/ledger.jsonl 47 --check

`--check` asserts the replayed blotter digest matches the one recorded, which is
what makes "every trial is reproducible" a claim rather than an aspiration. A
digest identifies a trial; the stored source is what reproduces it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.datasets.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.types import Split  # noqa: E402


def load_strategy_from(path: Path):
    spec = importlib.util.spec_from_file_location("replayed_strategy", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["replayed_strategy"] = module
    spec.loader.exec_module(module)
    for attr in ("STRATEGY", "Strategy", "MomentumStrategy"):
        obj = getattr(module, attr, None)
        if obj is not None:
            return obj() if isinstance(obj, type) else obj
    raise SystemExit(f"{path} defines no strategy class")


def replay(row: dict, cfg_path: Path = REPO_ROOT / "config" / "run.yaml"):
    import yaml

    cfg = yaml.safe_load(cfg_path.read_text())
    profile = cfg["profiles"][row["profile"]]
    name = profile["dataset"]
    spec = dict(cfg["datasets"][name])
    spec.pop("kind")
    spec["seed"] = row["seed"]
    dataset = SyntheticDataset(SyntheticSpec(**spec))
    if row.get("holdout_unlock_reason"):
        dataset.unlock_holdout(row["holdout_unlock_reason"])

    source = Path(row["strategy_source"])
    if not source.is_absolute():
        source = REPO_ROOT / source
    if not source.exists():
        raise SystemExit(
            f"the source for this trial is gone: {row['strategy_source']}. "
            "A digest identifies a trial; only the stored source reproduces it."
        )
    engine = Engine(
        dataset,
        load_strategy_from(source),
        split=Split(row["split"]),
        fill_model=row["fill_model"],
        warmup_sessions=row.get("warmup_sessions", 0),
        seed=row["seed"],
    )
    engine.run()
    return engine


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ledger", type=Path)
    ap.add_argument("row", type=int, help="0-based row index in the ledger")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    rows = [json.loads(x) for x in args.ledger.read_text().splitlines() if x.strip()]
    row = rows[args.row]
    engine = replay(row)
    got, want = engine.blotter.digest(), row["blotter_digest"]
    print(f"strategy   {row['strategy_source']}  ({row['strategy_digest']})")
    print(f"dataset    {row['dataset']}  ({row['dataset_digest']})")
    print(f"recorded   {want}")
    print(f"replayed   {got}")
    if got != want:
        print("MISMATCH", file=sys.stderr)
        return 1
    print("identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
