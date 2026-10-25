#!/usr/bin/env python3
"""Re-run a ledger row from the strategy source stored beside it.

    python scripts/replay.py runs/ledger.jsonl 47
    python scripts/replay.py runs/ledger.jsonl 47 --check

`--check` asserts the replayed blotter digest matches the one recorded, which is
what makes "every trial is reproducible" a claim rather than an aspiration. A
digest identifies a trial; the stored source is what reproduces it.

**Reproducing is not verifying, and the second adversarial pass made the
difference concrete.** A strategy that rewrote the engine's average price mid-run
replayed perfectly: replay re-ran the same mutation and got the same wrong
number, and the matching digest was evidence of determinism and nothing else. So
where the row carries a fill log, this also recomputes the P&L from that log with
`reconstruct` -- a pure function of the fills -- and checks the replayed run
produced the same fills. The fills are facts; the P&L is a derivation over them,
and a derivation you can redo independently is one you do not have to trust.
"""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.engine.reconstruct import log_digest, reconstruct  # noqa: E402
from src.core.runlog import LedgerRun  # noqa: E402
from src.core.types import DEFAULT_ASSET, FillRecord, Side, Split  # noqa: E402


def load_fill_log(row: dict) -> list[FillRecord] | None:
    """The fills the recorded run logged, if the row carried them.

    `None` rather than an empty list when there is no file: "no fills stored" and
    "the run made no trades" are different claims and only one of them can be
    checked.
    """
    source = row.get("fill_log_source")
    if not source:
        return None
    path = Path(source)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        return None
    fills = []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            # **Nine fields OR ten, and both are real.** `FillRecord` gained
            # `asset` in the multi-asset work, appended last, so every log
            # written since then has ten values and every log written before
            # has nine. This line unpacked exactly nine and raised
            # `ValueError: too many values to unpack` on the first options
            # campaign's `nulls` stage -- after all 50 trials had run. The
            # trials were fine; nothing could read their fills back.
            #
            # Reading both is not politeness to old data, it is the whole
            # reproduction story: a row is replayed against the code of ITS
            # commit, so a reader that only understands today's width cannot
            # verify anything written before today. This function is the single
            # parse point for `nulls`, `l2_gate`, `holdout` and `replay` --
            # including the one-look-ever holdout, which would have failed the
            # same way at the most expensive possible moment.
            row_fields = json.loads(line)
            (seq, ts, session, oid, side, size,
             price, liquidity, tag) = row_fields[:9]
            asset = row_fields[9] if len(row_fields) > 9 else DEFAULT_ASSET
            fills.append(FillRecord(
                seq=seq, ts=ts, session=session, order_id=oid, side=Side(side),
                size=size, price=price, liquidity=liquidity, tag=tag,
                asset=asset,
            ))
    return fills


def load_strategy_from(path: Path, origin: str = ""):
    """Import the stored source as the module it was, not as a loose file.

    `origin` is the row's `strategy` field -- the repo-relative path of the file
    that actually ran -- and it is here because loading without it silently
    stopped working. Task 17 gave the calibration reference a relative import
    (`from ...types import NS_PER_SECOND`); a module executed under the bare name
    `replayed_strategy` has no package, so the import raises and the row becomes
    unreplayable. Nothing noticed for five tasks, because nothing replayed a row
    in between. `scripts/ledger_check.py` is what noticed.

    So the module is given the package it lived in. A relative import then
    resolves exactly as it did during the run, which is the only way a replay can
    claim to be re-running the same code.
    """
    package = ""
    if origin.endswith(".py"):
        package = origin[:-3].replace("/", ".").rsplit(".", 1)[0]
    name = f"{package}._replayed" if package else "replayed_strategy"

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if package:
        module.__package__ = package
    sys.modules[name] = module
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
    elif family == "spx-heston":
        # Same rule as the synthetic branch and for the same reason: the ROW
        # names the data, and the committed config is searched for an entry
        # that rebuilds to that name. Matching on the profile's current
        # dataset would replay a planted surface against a clean one, which
        # produces a digest mismatch that reads as non-determinism.
        #
        # The name carries the plant count, the expiry grid, the surface
        # digest and the path seed, so an entry that reproduces it reproduces
        # the bytes. `build_dataset` is reused rather than reimplemented --
        # two constructions of one dataset is exactly how this function's
        # docstring says the last bug happened.
        import run as _run

        dataset = None
        for cname, candidate in cfg["datasets"].items():
            if candidate.get("kind") != "spx-options":
                continue
            trial = _run.build_dataset(cfg, cname, row["seed"])
            if trial.name == name:
                dataset = trial
                break
        if dataset is None:
            raise SystemExit(
                f"no committed dataset in config/run.yaml reconstructs {name!r}. "
                "The row names a surface this repository can no longer build."
            )
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


REPLAY_LOG = REPO_ROOT / "runs" / "replays.jsonl"


def replay(row: dict, cfg_path: Path = REPO_ROOT / "config" / "run.yaml",
           *, replay_log: Path | None = None):
    """Re-run the row. Logged, like every execution -- but not into the ledger.

    A replay executes strategy code, so Task 23's rule applies: no engine without
    a row. It does not belong in `runs/ledger.jsonl`, though. That file is the
    denominator of every false-discovery statement here, and a replay is not a
    candidate: the source is byte-fixed, the dataset, split, seed and fill model
    all come from the row, and there is no free parameter, so no selection can
    happen through it. Putting replays there would also make
    `scripts/ledger_check.py --replay` grow the file it is checking, once per row
    per pass.

    So they land in `runs/replays.jsonl`, append-only, same shape. Every
    execution is recorded; the two records answer different questions.
    """
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
    log_path = replay_log or REPLAY_LOG

    def build_row(record: dict) -> dict:
        return {
            **record,
            "purpose": "replay",
            "trigger": "human",
            "replay_of": {
                "blotter_digest": row.get("blotter_digest"),
                "strategy_digest": row.get("strategy_digest"),
                "git_sha": row.get("git_sha"),
            },
        }

    with LedgerRun(log_path, row_builder=build_row) as run_log:
        engine = Engine(
            dataset,
            load_strategy_from(source, row.get("strategy", "")),
            ticket=run_log.ticket,
            split=Split(row["split"]),
            fill_model=row["fill_model"],
            warmup_sessions=row.get("warmup_sessions", 0),
            seed=row["seed"],
        )
        engine.run()
        run_log.update(engine.record.as_dict())
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
    failed = got != want

    # the verification half: recompute, do not re-observe
    stored = load_fill_log(row)
    if stored is None:
        print("fills      not stored with this row; P&L is reproduced, not verified")
    else:
        replayed_digest = engine.fill_log_digest()
        recorded_digest = row.get("fill_log_digest", "")
        same_fills = log_digest(stored) == recorded_digest == replayed_digest
        rebuilt = reconstruct(stored, commission_per_contract=engine.commission)
        net = rebuilt.realized_pnl - rebuilt.commission
        print(f"fills      {len(stored):,}  digest {recorded_digest[:16]}  "
              f"{'same as replayed' if same_fills else 'DIFFERENT from replayed'}")
        print(f"P&L        {net:+.6f} recomputed from the log alone")
        if not same_fills:
            failed = True

    if failed:
        print("MISMATCH", file=sys.stderr)
        return 1
    print("identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
