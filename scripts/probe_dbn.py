#!/usr/bin/env python3
"""Inspect one DBN object: compression depth, header, record count, first rows.

Promoted from the throwaway probe that surfaced the ES-under-NQ contamination and
the double-zstd layering, so the next surprise can be diagnosed the same way
instead of being rewritten inline.

Accepts a local path, an `s3://bucket/key` URI, or a bare bucket key resolved
against `$R2_BUCKET`:

    python scripts/probe_dbn.py raw/trades/NQ/glbx-mdp3-20250703.trades.dbn.zst
    python scripts/probe_dbn.py raw/trades/NQ/glbx-mdp3-20250703.trades.dbn.zst --count
    python scripts/probe_dbn.py ./local/glbx-mdp3-20250703.trades.dbn.zst --rows 5

`--compare` takes two arbitrary objects and asserts they decode to the same
frame -- useful for checking a DBN v1 object against a v3 one, or a local
download against what is in the bucket:

    python scripts/probe_dbn.py raw/trades/NQ/glbx-mdp3-20250710.trades.dbn.zst \
                                raw/trades/NQ/glbx-mdp3-20250709.trades.dbn.zst --compare

Reads are ranged by default; `--count` streams the whole object to count records
exactly, so do not point it at `raw/opra_cbbo1m` casually (76 MB per day).
Credentials come from the environment and are never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import databento_dbn as ddbn  # noqa: E402

import inventory as inv  # noqa: E402


def is_local(target: str) -> bool:
    return Path(target).exists()


def split_uri(target: str) -> tuple[str, str]:
    if target.startswith("s3://"):
        bucket, _, key = target[5:].partition("/")
        return bucket, key
    bucket = os.environ.get("R2_BUCKET")
    if not bucket:
        sys.exit("R2_BUCKET is not set and the target is not a local path or s3:// URI")
    return bucket, target


class LocalFetcher:
    """Mimics the slice of the boto3 client that inventory's helpers use, so the
    same code path serves a local file and a bucket object."""

    def __init__(self, path: Path):
        self.path = path

    def get_object(self, Bucket=None, Key=None, Range=None):  # noqa: N803
        data = self.path.read_bytes()
        if Range:
            lo, hi = Range.removeprefix("bytes=").split("-")
            data = data[int(lo) : int(hi) + 1]
        return {"Body": io.BytesIO(data)}


def open_target(target: str):
    if is_local(target):
        p = Path(target)
        return LocalFetcher(p), "", target, p.stat().st_size
    bucket, key = split_uri(target)
    s3 = inv.s3_client(8)
    size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    return s3, bucket, key, size


def describe(target: str, rows: int, count: bool, digest: bool) -> dict:
    s3, bucket, key, size = open_target(target)
    meta = inv.dbn_metadata(s3, bucket, key, size)

    print(f"target                {target}")
    print(f"compressed size       {size:,} bytes ({inv.human_bytes(size)})")
    print(f"zstd layers           {meta['compression_layers']}"
          + ("   <- decompress more than once" if meta["compression_layers"] > 1 else ""))
    print(f"dbn version           v{meta['dbn_version']}")
    print(f"dataset / schema      {meta['dataset']} / {meta['schema']}")
    print(f"symbology             stype_in={meta['stype_in']} -> stype_out={meta['stype_out']}"
          f"  ts_out={meta['ts_out']}")
    print(f"query symbols         {', '.join(meta['query_symbols']) or '(none)'}")
    print(f"query window (ns)     {meta['query_start_ns']} -> {meta['query_end_ns']}")
    print(f"instruments in header {meta['instrument_count']:,}")
    print(f"record width          {meta['record_size']} bytes")
    print(f"metadata block        {meta['metadata_bytes']:,} bytes")

    if digest:
        if isinstance(s3, LocalFetcher):
            print(f"sha256                {hashlib.sha256(Path(target).read_bytes()).hexdigest()}")
        else:
            print(f"sha256                {inv.sha256_of(s3, bucket, key)}")

    if count:
        c = inv.count_records(s3, bucket, key)
        print(f"records (exact)       {c['records']:,}")
        print(f"decompressed          {c['decompressed_bytes']:,} bytes "
              f"({inv.human_bytes(c['decompressed_bytes'])})")
        if c["fixed_width_remainder"]:
            print(f"WARNING               payload is not a multiple of the record width "
                  f"(remainder {c['fixed_width_remainder']} bytes): the file mixes record types")

    df = inv.read_head_df(s3, bucket, key)
    print()
    print(f"index                 {df.index.name} {df.index.dtype}")
    print(f"columns ({len(df.columns)})          {', '.join(map(str, df.columns))}")
    if rows:
        print()
        print(df.head(rows).to_string())
    return {"meta": meta, "columns": list(map(str, df.columns)), "dtypes": df.dtypes}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("target", nargs="+", help="local path, s3:// URI, or bucket key")
    ap.add_argument("--rows", type=int, default=3, help="rows of the decoded head to print")
    ap.add_argument("--count", action="store_true",
                    help="stream the whole object for an exact record count")
    ap.add_argument("--sha256", action="store_true", help="also print the object digest")
    ap.add_argument("--compare", action="store_true",
                    help="with two targets, assert their decoded schemas match")
    args = ap.parse_args()

    results = []
    for i, t in enumerate(args.target):
        if i:
            print("\n" + "-" * 72 + "\n")
        results.append(describe(t, args.rows, args.count, args.sha256))

    if args.compare:
        if len(results) != 2:
            sys.exit("--compare needs exactly two targets")
        a, b = results
        same_cols = a["columns"] == b["columns"]
        same_dtypes = a["dtypes"].equals(b["dtypes"])
        print("\n" + "=" * 72)
        print(f"columns identical     {same_cols}")
        print(f"dtypes identical      {same_dtypes}")
        if not (same_cols and same_dtypes):
            print("differing columns     "
                  f"{sorted(set(a['columns']) ^ set(b['columns'])) or '(none)'}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
