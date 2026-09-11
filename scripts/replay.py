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

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
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
    # The harness runs immutable apparatus as well as the agent's file, and the
    # calibration reference is not called `Strategy`. Anything in the module that
    # looks like one will do: replay's job is to re-run what ran, and the row
    # already says which file that was.
    candidates = [
        obj for name, obj in vars(module).items()
        if isinstance(obj, type) and not name.startswith("_")
        and callable(getattr(obj, "setup", None))
        and callable(getattr(obj, "on_bars", None))
    ]
    if len(candidates) == 1:
        return candidates[0]()
    raise SystemExit(
        f"{path} defines {len(candidates)} strategy classes; replay needs exactly "
        "one to know what ran"
    )


def rebuild_dataset(row: dict, cfg: dict):
    """The dataset the row names, at the digest the row records.

    It used to reconstruct a synthetic dataset from whatever profile the row
    mentioned, which worked for every row that existed when it was written and
    for none that did not: the `nq-smoke` row died with `KeyError: 'nq-smoke'`,
    and a row whose profile pointed at a different dataset replayed against the
    wrong data and produced a digest mismatch that looked like non-determinism.
    "Any trial can be replayed from its stored source" was true on synthetic and
    false for the one real row in the ledger.

    So the row is the authority, not the config: `dataset` names the adapter and
    `dataset_digest` pins the bytes. A digest that no longer matches is a
    refusal, loudly -- replaying against data that has since changed is worse
    than not replaying, because it produces a number.
    """
    import os

    name = row["dataset"]
    family = name.split(":", 1)[0]

    if family == "synthetic":
        # `synthetic:s=0:seed=1` carries its own parameters in the name, but not
        # all of them; the committed dataset entry supplies the rest. Match by
        # what the name says rather than by the profile's current choice.
        spec = None
        for candidate in cfg["datasets"].values():
            if candidate.get("kind") != "synthetic":
                continue
            trial = dict(candidate)
            trial.pop("kind")
            trial["seed"] = row["seed"]
            if SyntheticDataset(SyntheticSpec(**trial)).name == name:
                spec = trial
                break
        if spec is None:
            raise SystemExit(
                f"no committed dataset in config/run.yaml reconstructs {name!r}. "
                "The row names data this repository can no longer build."
            )
        dataset = SyntheticDataset(SyntheticSpec(**spec))
    elif family == "nq-trades":
        import boto3

        from src.core.data.nq import NQDataset, NQSpec

        missing = [v for v in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID",
                               "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
                   if not os.environ.get(v)]
        if missing:
            raise SystemExit(
                f"replaying {name!r} needs read-only bucket credentials; missing "
                + ", ".join(missing)
            )
        client = boto3.client(
            "s3", endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
        symbol = name.split(":", 1)[1] if ":" in name else "NQ"
        dataset = NQDataset(client, os.environ["R2_BUCKET"], NQSpec(symbol=symbol))
    else:
        raise SystemExit(f"no adapter can rebuild dataset {name!r}")

    recorded = row.get("dataset_digest")
    if recorded and dataset.digest() != recorded:
        raise SystemExit(
            f"{name} no longer hashes to what the row recorded: "
            f"{recorded} then, {dataset.digest()} now. The data has changed, so "
            "this trial cannot be reproduced -- which is the answer, not an error "
            "to work around."
        )
    return dataset


def replay(row: dict, cfg_path: Path = REPO_ROOT / "config" / "run.yaml"):
    import yaml

    cfg = yaml.safe_load(cfg_path.read_text())
    dataset = rebuild_dataset(row, cfg)
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
