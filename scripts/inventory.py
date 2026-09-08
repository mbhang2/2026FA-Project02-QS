#!/usr/bin/env python3
"""Data inventory for the Cloudflare R2 bucket backing the L1 research loop.

Reads the bucket, works out how it is partitioned, what is in it and what is
missing, then writes two artefacts:

    data/manifest.json    machine-readable, committed
    docs/data-inventory.md  human-readable summary, committed

Deterministic: object listing, sampling and all output ordering are sorted or
strided, never random, so re-running produces byte-identical files. The only
dates in the output are the data's own.

Cost discipline: the bucket holds ~45 GiB and `raw/opra_cbbo1m` alone is 38 GiB.
We never scan it. Per-file metadata comes from ranged reads of the DBN header
(the DBN equivalent of a parquet footer); exact record counts come from a small
deterministic sample of days per prefix and are extrapolated to prefix totals,
clearly labelled as estimates.

Credentials come from the environment (direnv / secrets/r2.env) and are never
printed or written to either output.

Usage:
    python scripts/inventory.py                 # uses cache when present
    python scripts/inventory.py --refresh       # ignore cache, re-probe bucket
    python scripts/inventory.py --help
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

import boto3
import botocore.config
import databento_dbn as ddbn
import pandas as pd
import pandas_market_calendars as mcal
import zstandard

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "data" / "manifest.json"
DOC_PATH = REPO_ROOT / "docs" / "data-inventory.md"
CACHE_DIR = REPO_ROOT / ".scratch" / "inventory-cache"

# Bump the relevant entry when a probe function changes shape or fixes a bug, so
# stale cache entries from an older version of this script are discarded rather
# than reused. Versions are per-cache so one change does not invalidate the rest.
CACHE_VERSIONS = {"dbn_metadata": 2, "record_counts": 2, "frame_profiles": 3}

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
DBN_MAGIC = b"DBN"

# Ranged read big enough to hold the DBN metadata block of every prefix in this
# bucket (largest observed: 386 KiB uncompressed, ~56 KiB compressed). Grown
# adaptively when a file needs more.
HEAD_BYTES = 96 * 1024

# CME session boundary. Everything in this repo is reasoned about in these terms.
CME_TZ = "America/Chicago"
SESSION_OPEN_HOUR = 17  # 17:00 CT Globex open; session date rolls here.

# Deterministic sample sizes per prefix, chosen to bound transfer:
#   trades  ~5.6 MB/file, opra_definition ~0.3 MB, opra_statistics ~3.1 MB,
#   opra_cbbo1m ~76 MB (one file only -- the task forbids scanning it).
SAMPLE_COUNTS = {
    "raw/trades": 10,  # per symbol
    "raw/opra_definition": 6,
    "raw/opra_statistics": 6,
    "raw/opra_cbbo1m": 2,
}

# Which market calendar governs which prefix.
CALENDARS = {
    "raw/trades": "CME_Equity",
    "raw/opra_cbbo1m": "CBOE_Equity_Options",
    "raw/opra_definition": "CBOE_Equity_Options",
    "raw/opra_statistics": "CBOE_Equity_Options",
}

KEY_RE = re.compile(
    r"^raw/(?P<prefix>[a-z0-9_]+)/"
    r"(?:(?P<symbol>[A-Z]{1,4})/)?"
    r"(?P<stem>[a-z0-9-]+)-(?P<date>\d{8})\.(?P<kind>[a-z0-9-]+)\.dbn\.zst$"
)


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #
def s3_client(max_pool: int = 64):
    missing = [
        v
        for v in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
        if not os.environ.get(v)
    ]
    if missing:
        sys.exit(
            f"missing environment variable(s): {', '.join(missing)}\n"
            "Run `direnv allow` in the repo root (see docs/setup/R2_ACCESS.md)."
        )
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=botocore.config.Config(
            max_pool_connections=max_pool,
            connect_timeout=20,
            read_timeout=120,
            retries={"max_attempts": 8, "mode": "adaptive"},
        ),
    )


def with_retry(fn, *a, attempts: int = 5, **kw):
    """Ranged reads against R2 occasionally time out under concurrency; botocore's
    own retries do not cover a stall while streaming the body."""
    last = None
    for i in range(attempts):
        try:
            return fn(*a, **kw)
        except Exception as exc:  # noqa: BLE001 - retry anything transport-shaped
            last = exc
            time.sleep(min(2**i, 15))
    raise last


class Cache:
    """Per-object probe cache keyed by (key, size). Purely a speed-up: deleting
    .scratch/ changes nothing about the output."""

    def __init__(self, name: str, enabled: bool = True):
        self.version = CACHE_VERSIONS[name]
        self.path = CACHE_DIR / f"{name}.json"
        self.enabled = enabled
        self.data: dict[str, Any] = {}
        if enabled and self.path.exists():
            self.data = json.loads(self.path.read_text())

    def get(self, key: str, size: int):
        hit = self.data.get(key)
        if hit is not None and hit.get("_size") == size and hit.get("_v") == self.version:
            return hit
        return None

    def put(self, key: str, size: int, value: dict):
        value["_size"] = size
        value["_v"] = self.version
        self.data[key] = value

    def flush(self):
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, sort_keys=True))


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# DBN reading
# --------------------------------------------------------------------------- #
def _decompress_partial(buf: bytes, limit: int) -> bytes:
    """Decompress up to `limit` bytes, spanning frames, tolerating a truncated tail.

    Databento writes the DBN metadata block in its own zstd frame and then the
    records in a long run of ~64 KiB frames, so a single-frame decode stops
    short of the first record. A ranged read also ends mid-frame, which raises
    -- we keep whatever decoded before that.
    """
    out = io.BytesIO()
    reader = zstandard.ZstdDecompressor().stream_reader(
        io.BytesIO(buf), read_across_frames=True
    )
    try:
        while out.tell() < limit:
            chunk = reader.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
    except zstandard.ZstdError:
        pass
    return out.getvalue()


def unwrap_zstd(buf: bytes, limit: int = 1 << 21, max_layers: int = 3) -> tuple[bytes, int]:
    """Decompress until DBN magic appears.

    Two of the four prefixes in this bucket are zstd-compressed twice (the
    mirror re-compressed an already-compressed Databento file), so a single
    decompress is not enough.
    """
    layers = 0
    while buf[:4] == ZSTD_MAGIC and layers < max_layers:
        out = _decompress_partial(buf, limit)
        if not out:
            break
        buf, layers = out, layers + 1
        if buf[:3] == DBN_MAGIC:
            break
    return buf, layers


def dbn_metadata(s3, bucket: str, key: str, size: int) -> dict:
    """Read one object's DBN metadata block using ranged reads only."""
    want = HEAD_BYTES
    while True:
        end = min(want, size) - 1
        raw = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{end}")["Body"].read()
        buf, layers = unwrap_zstd(raw)
        if buf[:3] != DBN_MAGIC:
            if want >= size:
                raise ValueError(f"{key}: no DBN header found")
            want *= 4
            continue
        mlen = int.from_bytes(buf[4:8], "little")
        # +4 so the first record's length byte is in hand as well
        if len(buf) >= 8 + mlen + 4 or want >= size:
            break
        want *= 4

    m = ddbn.Metadata.decode(buf[: 8 + mlen], upgrade_policy=ddbn.VersionUpgradePolicy.AS_IS)
    # First record after the metadata block tells us the fixed record width.
    rest = buf[8 + mlen :]
    record_size = rest[0] * 4 if rest else None
    return {
        "dbn_version": buf[3],
        "compression_layers": layers,
        "dataset": str(m.dataset),
        "schema": str(m.schema),
        "stype_in": str(m.stype_in),
        "stype_out": str(m.stype_out),
        "ts_out": bool(m.ts_out),
        "query_symbols": list(m.symbols),
        "query_start_ns": int(m.start),
        "query_end_ns": int(m.end),
        "instrument_count": len(m.mappings),
        "metadata_bytes": mlen,
        "record_size": record_size,
    }


def count_records(s3, bucket: str, key: str) -> dict:
    """Exact record count for one object, streamed, O(1) memory.

    All DBN schemas used here are fixed-width, so the count follows from the
    decompressed byte count without decoding a single record. The divisibility
    check below is what makes that safe: if the file contains variable-width
    records (e.g. interleaved symbol-mapping or error messages) the remainder is
    non-zero and we say so rather than reporting a wrong number.
    """
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    dctx = zstandard.ZstdDecompressor()
    reader = dctx.stream_reader(body, read_across_frames=True)

    head = reader.read(1 << 20)
    inner_layers = 1
    if head[:4] == ZSTD_MAGIC:
        # Double-compressed prefix: rest of the outer stream is one inner stream.
        inner = io.BytesIO(head + reader.read())
        reader = zstandard.ZstdDecompressor().stream_reader(inner, read_across_frames=True)
        head = reader.read(1 << 20)
        inner_layers = 2
    if head[:3] != DBN_MAGIC:
        raise ValueError(f"{key}: no DBN header after decompression")

    mlen = int.from_bytes(head[4:8], "little")
    header_bytes = 8 + mlen
    record_size = head[header_bytes] * 4

    total = len(head)
    while True:
        chunk = reader.read(1 << 22)
        if not chunk:
            break
        total += len(chunk)

    payload = total - header_bytes
    remainder = payload % record_size
    return {
        "decompressed_bytes": total,
        "header_bytes": header_bytes,
        "record_size": record_size,
        "records": payload // record_size,
        "fixed_width_remainder": remainder,
        "compression_layers": inner_layers,
    }


def read_head_df(s3, bucket: str, key: str, max_bytes: int = 8 << 20) -> pd.DataFrame:
    """Decode the first whole records of an object from a ranged read.

    Used for `raw/opra_cbbo1m`, where a full read is 76 MB per day and the task
    forbids scanning the prefix. Enough for column names and dtypes; not enough
    for a session profile, which is why that is reported only for `trades`.
    """
    import databento as db

    raw = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{max_bytes - 1}")["Body"].read()
    buf, _ = unwrap_zstd(raw, limit=max_bytes * 8)
    mlen = int.from_bytes(buf[4:8], "little")
    header = 8 + mlen
    rec = buf[header] * 4
    whole = header + ((len(buf) - header) // rec) * rec
    return db.DBNStore.from_bytes(buf[:whole]).to_df()


def read_full_df(s3, bucket: str, key: str) -> pd.DataFrame:
    """Whole-file read to a DataFrame. Only ever called on sampled `trades` days
    and on the small `opra_definition` files."""
    import databento as db

    raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    buf, _ = unwrap_zstd(raw, limit=1 << 62)
    return db.DBNStore.from_bytes(buf).to_df()


# --------------------------------------------------------------------------- #
# key parsing
# --------------------------------------------------------------------------- #
def parse_key(key: str) -> dict | None:
    m = KEY_RE.match(key)
    if not m:
        return None
    d = m.groupdict()
    ymd = d["date"]
    return {
        "key": key,
        "prefix": f"raw/{d['prefix']}",
        "folder_symbol": d["symbol"],
        "file_date": dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:])),
        "stem": d["stem"],
        "kind": d["kind"],
    }


def list_objects(s3, bucket: str) -> list[dict]:
    out: list[dict] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            out.append({"key": o["Key"], "size": o["Size"]})
    return sorted(out, key=lambda r: r["key"])


# --------------------------------------------------------------------------- #
# calendars
# --------------------------------------------------------------------------- #
def trading_days(calendar: str, start: dt.date, end: dt.date) -> tuple[list[dt.date], dict]:
    cal = mcal.get_calendar(calendar)
    sched = cal.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    days = [d.date() for d in sched.index]
    # Early closes show up as size outliers downstream; record them so a small
    # file on a half day is not mistaken for a truncated one.
    closes = sched["market_close"].dt.tz_convert(CME_TZ)
    by_time = Counter(closes.dt.strftime("%H:%M"))
    normal = by_time.most_common(1)[0][0] if len(by_time) else None
    early = sorted(
        d.date().isoformat()
        for d, t in zip(sched.index, closes.dt.strftime("%H:%M"))
        if t != normal
    )
    return days, {"normal_close_ct": normal, "early_close_days": early}


def expected_utc_file_dates(sessions: Iterable[dt.date]) -> set[dt.date]:
    """Map CME session dates to the UTC dates whose files must exist.

    Databento splits GLBX files on UTC day, but a CME session runs 17:00 CT the
    previous calendar day to 16:00 CT on the session date. 17:00 CT is 22:00/23:00
    UTC *the same evening*, so every session touches two UTC dates: its own and
    the day before. That is why the bucket has Sunday-dated files and why one
    session is never contained in one file.
    """
    out: set[dt.date] = set()
    for d in sessions:
        out.add(d)
        out.add(d - dt.timedelta(days=1))
    return out


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #
def size_stats(sizes: list[int]) -> dict:
    s = sorted(sizes)
    q = lambda p: s[min(len(s) - 1, int(round(p * (len(s) - 1))))]
    return {
        "min": s[0],
        "p05": q(0.05),
        "p25": q(0.25),
        "median": q(0.50),
        "p75": q(0.75),
        "p95": q(0.95),
        "max": s[-1],
        "mean": int(sum(s) / len(s)),
    }


def size_outliers(
    rows: list[dict],
    sessions: set[dt.date],
    early_close: set[str],
    window: int = 11,
    low: float = 0.10,
    high: float = 4.0,
) -> list[dict]:
    """Files far from the median of their *neighbours* in date order.

    A prefix-wide median would flag every Sunday and every holiday eve, and would
    miss a truncated file in a stretch that is uniformly small. `low=0.10` is the
    '10x smaller than its neighbours' truncation check from the task.

    `explained_by` separates the structurally small files from the suspicious
    ones. A file whose date is not itself a trading session, but which exists
    because the next session opens at 17:00 CT the evening before, can only ever
    hold those two evening hours -- that covers Sundays and holiday eves alike.
    """
    ordered = sorted(rows, key=lambda r: r["file_date"])
    half = window // 2
    out = []
    for i, r in enumerate(ordered):
        lo, hi = max(0, i - half), min(len(ordered), i + half + 1)
        neighbours = [x["size"] for j, x in enumerate(ordered[lo:hi], lo) if j != i]
        med = statistics.median(neighbours) if neighbours else r["size"]
        ratio = r["size"] / med if med else 0.0
        if low < ratio < high:
            continue
        d = r["file_date"].isoformat()
        if r["file_date"] not in sessions:
            why = "evening_open_only"  # no session on this date; only the 17:00 CT open
        elif d in early_close:
            why = "early_close"
        else:
            why = None
        out.append(
            {
                "key": r["key"],
                "date": d,
                "size_bytes": r["size"],
                "neighbour_median_bytes": int(med),
                "ratio_to_neighbour_median": round(ratio, 4),
                "flag": "small" if ratio <= low else "large",
                "explained_by": why,
            }
        )
    return sorted(out, key=lambda r: (r["date"], r["key"]))


def stride_sample(items: list, n: int) -> list:
    """Deterministic, evenly spread sample. Positions are centred in their bucket
    so the first element -- frequently a half session -- is not always picked."""
    if n <= 0 or not items:
        return []
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    idx = sorted({min(len(items) - 1, int((i + 0.5) * step)) for i in range(n)})
    return [items[i] for i in idx]


def weekday_class(d: dt.date) -> str:
    """The four shapes a GLBX UTC-day file takes.

    Sunday holds only the weekly 17:00 CT open; Friday stops at the 16:00 CT
    weekly close with no evening reopen; midweek files run a full 24 hours, whose
    CT boundaries shift by an hour between CDT and CST.
    """
    if d.weekday() == 6:
        return "sunday"
    if d.weekday() == 4:
        return "friday"
    is_dst = bool(
        pd.Timestamp(d.isoformat(), tz=CME_TZ).dst()
    )
    return "midweek_dst" if is_dst else "midweek_std"


def session_profile(df: pd.DataFrame) -> dict:
    """Where in the CME day do this file's timestamps actually fall?"""
    idx = df.index
    ct = idx.tz_convert(CME_TZ)
    hours = Counter(int(h) for h in ct.hour)
    return {
        "rows": int(len(df)),
        "ts_first_utc": idx.min().isoformat(),
        "ts_last_utc": idx.max().isoformat(),
        "ts_first_ct": ct.min().isoformat(),
        "ts_last_ct": ct.max().isoformat(),
        "ct_hour_histogram": {str(h): hours[h] for h in sorted(hours)},
        "covers_globex_open_1700ct": 17 in hours,
        "covers_rth_0830_1500ct": any(h in hours for h in range(9, 15)),
        "distinct_ct_hours": len(hours),
    }


def schema_of(df: pd.DataFrame) -> dict:
    # A list, not a dict: the manifest is written with sort_keys=True and DBN
    # field order carries meaning, so it must survive serialisation.
    cols = []
    for name, dtype in df.dtypes.items():
        entry = {"name": str(name), "dtype": str(dtype)}
        if str(dtype).startswith("datetime64"):
            entry["unit"] = str(dtype).split("[")[1].split(",")[0].strip()
            entry["tz"] = str(getattr(df[name].dt, "tz", None))
        cols.append(entry)
    idx = {
        "name": str(df.index.name),
        "dtype": str(df.index.dtype),
        "tz": str(getattr(df.index, "tz", None)),
    }
    if str(df.index.dtype).startswith("datetime64"):
        idx["unit"] = str(df.index.dtype).split("[")[1].split(",")[0].strip()
    return {"index": idx, "columns": cols}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build(args) -> dict:
    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client(args.workers)

    log("listing bucket ...")
    objects = list_objects(s3, bucket)

    parsed, unparsed = [], []
    for o in objects:
        p = parse_key(o["key"])
        if p:
            p["size"] = o["size"]
            parsed.append(p)
        else:
            unparsed.append(o)

    by_prefix: dict[str, list[dict]] = defaultdict(list)
    for r in parsed:
        by_prefix[r["prefix"]].append(r)
    for rows in by_prefix.values():
        rows.sort(key=lambda r: r["key"])

    # ---- per-object DBN metadata (ranged reads, cached) -------------------- #
    cache = Cache("dbn_metadata", enabled=not args.no_cache)
    def stale(r):
        hit = cache.get(r["key"], r["size"])
        return hit is None or hit.get("record_size") is None

    todo = [r for r in parsed if args.refresh or stale(r)]
    if todo:
        log(f"probing DBN headers for {len(todo)} object(s) (cached: {len(parsed) - len(todo)}) ...")

        def probe(r):
            meta = with_retry(dbn_metadata, s3, bucket, r["key"], r["size"])
            return r["key"], r["size"], meta

        with ThreadPoolExecutor(args.workers) as ex:
            for i, (key, size, meta) in enumerate(ex.map(probe, todo), 1):
                cache.put(key, size, meta)
                if i % 200 == 0:
                    cache.flush()  # checkpoint: a network wobble should not cost the run
                    log(f"  {i}/{len(todo)}")
        cache.flush()
    for r in parsed:
        r["meta"] = cache.get(r["key"], r["size"])

    # ---- exact record counts on a deterministic sample --------------------- #
    ccache = Cache("record_counts", enabled=not args.no_cache)
    sample_keys: list[str] = []
    for prefix, rows in sorted(by_prefix.items()):
        if prefix == "raw/trades":
            for sym in sorted({r["folder_symbol"] for r in rows}):
                srows = [r for r in rows if r["folder_symbol"] == sym]
                sample_keys += [r["key"] for r in stride_sample(srows, SAMPLE_COUNTS[prefix])]
        else:
            sample_keys += [r["key"] for r in stride_sample(rows, SAMPLE_COUNTS.get(prefix, 4))]
    sample_keys = sorted(set(sample_keys))
    sample_key_set = set(sample_keys)
    size_by_key = {r["key"]: r["size"] for r in parsed}

    pending = [k for k in sample_keys if args.refresh or ccache.get(k, size_by_key[k]) is None]
    if pending:
        log(f"counting records in {len(pending)} sampled file(s) ...")
        with ThreadPoolExecutor(min(8, args.workers)) as ex:
            counter = lambda k: with_retry(count_records, s3, bucket, k)
            for key, res in zip(pending, ex.map(counter, pending)):
                ccache.put(key, size_by_key[key], res)
                log(f"  {key}: {res['records']:,} records")
        ccache.flush()

    # ---- schema + session profile from full reads -------------------------- #
    scache = Cache("frame_profiles", enabled=not args.no_cache)
    profile_keys: list[str] = []
    head_only: set[str] = set()
    for prefix, rows in sorted(by_prefix.items()):
        if prefix == "raw/trades":
            # One sample per (symbol, weekday-class) so the session profile shows a
            # Sunday open-only file, a Friday close-out file and ordinary midweek
            # days in both DST regimes -- the four shapes a bar builder must handle.
            for sym in sorted({r["folder_symbol"] for r in rows}):
                srows = [r for r in rows if r["folder_symbol"] == sym]
                for cls in ("sunday", "friday", "midweek_dst", "midweek_std"):
                    pool = [
                        r
                        for r in srows
                        if weekday_class(r["file_date"]) == cls
                        # never profile a file whose header names another root: it
                        # would be presented as this symbol's data
                        and r["meta"]["query_symbols"]
                        and r["meta"]["query_symbols"][0].split(".")[0] == sym
                    ]
                    profile_keys += [r["key"] for r in stride_sample(pool, 1)]
        elif prefix == "raw/opra_cbbo1m":
            # 76 MB/day: take the schema from a ranged read, never the whole file.
            for r in stride_sample(rows, 1):
                profile_keys.append(r["key"])
                head_only.add(r["key"])
        else:
            profile_keys += [r["key"] for r in stride_sample(rows, 1)]
    profile_keys = sorted(set(profile_keys))
    pending = [k for k in profile_keys if args.refresh or scache.get(k, size_by_key[k]) is None]
    if pending:
        log(f"reading {len(pending)} file(s) for schema + session profile ...")
        for k in pending:
            reader = read_head_df if k in head_only else read_full_df
            df = with_retry(reader, s3, bucket, k)
            prof = {"schema": schema_of(df), "partial_read": k in head_only}
            if str(df.index.dtype).startswith("datetime64") and k not in head_only:
                prof.update(session_profile(df))
            if "symbol" in df.columns:
                vc = df["symbol"].value_counts()
                prof["contract_symbols"] = {str(a): int(b) for a, b in vc.head(20).items()}
                prof["distinct_contract_symbols"] = int(df["symbol"].nunique())
                prof["spread_rows"] = int(sum("-" in str(s) for s in df["symbol"]))
            scache.put(k, size_by_key[k], prof)
            log(f"  {k}")
        scache.flush()

    # ---- assemble ---------------------------------------------------------- #
    prefixes: dict[str, Any] = {}
    anomalies: list[dict] = []

    for prefix, rows in sorted(by_prefix.items()):
        dates = sorted(r["file_date"] for r in rows)
        cal_name = CALENDARS[prefix]
        sessions, cal_info = trading_days(cal_name, dates[0], dates[-1])
        early = set(cal_info["early_close_days"])

        # symbol grouping: folder for trades, the DBN query symbol otherwise
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            groups[r["folder_symbol"] or (r["meta"]["query_symbols"] or ["<none>"])[0]].append(r)

        # schema consistency across the whole prefix, from the cheap headers
        header_variants = Counter(
            (
                r["meta"]["dataset"],
                r["meta"]["schema"],
                r["meta"]["stype_in"],
                r["meta"]["stype_out"],
                r["meta"]["dbn_version"],
                r["meta"]["compression_layers"],
                r["meta"]["record_size"],
                tuple(r["meta"]["query_symbols"]),
            )
            for r in rows
        )

        sym_entries = {}
        for sym in sorted(groups):
            srows = sorted(groups[sym], key=lambda r: r["file_date"])
            sdates = [r["file_date"] for r in srows]
            if prefix == "raw/trades":
                span_sessions = [d for d in sessions if sdates[0] <= d <= sdates[-1]]
                expected = expected_utc_file_dates(span_sessions)
                expected = {d for d in expected if sdates[0] <= d <= sdates[-1]}
            else:
                expected = {d for d in sessions if sdates[0] <= d <= sdates[-1]}
            present = set(sdates)
            missing = sorted(expected - present)
            unexpected = sorted(present - expected)

            in_sample = [
                r
                for r in srows
                if r["key"] in sample_key_set and ccache.get(r["key"], r["size"])
            ]
            counted = [ccache.get(r["key"], r["size"]) for r in in_sample]
            sampled_rows = sum(c["records"] for c in counted)
            sampled_bytes = sum(r["size"] for r in in_sample)
            total_bytes = sum(r["size"] for r in srows)
            per_byte = sampled_rows / sampled_bytes if sampled_bytes else None

            # actual content symbols, straight from each file's own DBN header
            content = Counter(tuple(r["meta"]["query_symbols"]) for r in srows)
            content_by_date = defaultdict(list)
            for r in srows:
                content_by_date[tuple(r["meta"]["query_symbols"])].append(r["file_date"])

            # A file whose DBN header names a different root than its folder does
            # not provide data for this symbol, however it is named. Coverage the
            # research loop can actually use is the folder minus those files.
            mismatched = [
                r
                for r in srows
                if prefix == "raw/trades"
                and r["meta"]["query_symbols"]
                and r["meta"]["query_symbols"][0].split(".")[0] != sym
            ]
            mismatched_dates = sorted(r["file_date"] for r in mismatched)
            usable = present - set(mismatched_dates)
            content_missing = sorted(expected - usable)
            session_set = set(sessions)

            def classify(d: dt.date) -> str:
                if d not in session_set:
                    return "evening_open_only_date"
                if d.isoformat() in early:
                    return "half_session_date"
                return "full_session_date"

            # A shortened session that is absent here but present on every other
            # shortened session in range is a vendor gap, not a calendar quirk.
            half_in_range = [
                e for e in sorted(early) if sdates[0].isoformat() <= e <= sdates[-1].isoformat()
            ]
            half_present = sum(
                1 for e in half_in_range if dt.date.fromisoformat(e) in present
            )

            sym_entries[sym] = {
                "objects": len(srows),
                "total_bytes": total_bytes,
                "first_file_date": sdates[0].isoformat(),
                "last_file_date": sdates[-1].isoformat(),
                "distinct_file_dates": len(present),
                "expected_file_dates": len(expected),
                "missing_file_dates": [d.isoformat() for d in missing],
                "missing_count": len(missing),
                "missing_file_dates_classified": {
                    d.isoformat(): classify(d) for d in missing
                },
                "half_session_dates_in_range": len(half_in_range),
                "half_session_dates_present": half_present,
                "files_with_foreign_content": {
                    "count": len(mismatched),
                    "dates": [d.isoformat() for d in mismatched_dates],
                },
                "usable_file_dates": len(usable),
                "content_missing_dates": [d.isoformat() for d in content_missing],
                "content_missing_count": len(content_missing),
                "one_line_coverage": (
                    f"{sym}: {len(usable)} usable file-dates covering "
                    f"{min(usable).isoformat() if usable else '-'} to "
                    f"{max(usable).isoformat() if usable else '-'}, "
                    f"{len(content_missing)} expected date(s) absent"
                    + (
                        f" (including {len(mismatched_dates)} present-but-foreign: "
                        f"{mismatched_dates[0].isoformat()} to {mismatched_dates[-1].isoformat()})"
                        if mismatched_dates
                        else ""
                    )
                ),
                "unexpected_file_dates": [d.isoformat() for d in unexpected],
                "dbn_query_symbols": {
                    "|".join(k): {
                        "files": v,
                        "first_date": min(content_by_date[k]).isoformat(),
                        "last_date": max(content_by_date[k]).isoformat(),
                    }
                    for k, v in sorted(content.items())
                },
                "row_counts": {
                    "sampled_files": len(counted),
                    "sampled_bytes": sampled_bytes,
                    "sampled_rows_exact": sampled_rows,
                    "rows_per_compressed_byte": round(per_byte, 6) if per_byte else None,
                    "estimated_total_rows": int(round(per_byte * total_bytes)) if per_byte else None,
                    "method": "exact count on a strided sample, extrapolated by compressed bytes",
                },
                "size_bytes": size_stats([r["size"] for r in srows]),
                "size_outliers": size_outliers(srows, set(sessions), early),
            }

            # folder label vs actual content
            if prefix == "raw/trades":
                meta_by_key = {r["key"]: r["meta"] for r in rows}
                for qs, dl in sorted(content_by_date.items()):
                    root = qs[0].split(".")[0] if qs else None
                    if not root or root == sym:
                        continue
                    # Is the same content also sitting in the folder it belongs to?
                    dupes = []
                    for d in sorted(dl):
                        here = f"{prefix}/{sym}/glbx-mdp3-{d:%Y%m%d}.trades.dbn.zst"
                        there = f"{prefix}/{root}/glbx-mdp3-{d:%Y%m%d}.trades.dbn.zst"
                        a, bm = meta_by_key.get(here), meta_by_key.get(there)
                        if a and bm and all(
                            a[f] == bm[f]
                            for f in (
                                "query_symbols",
                                "query_start_ns",
                                "query_end_ns",
                                "instrument_count",
                                "metadata_bytes",
                            )
                        ):
                            dupes.append(d.isoformat())
                    anomalies.append(
                        {
                            "severity": "critical",
                            "prefix": prefix,
                            "kind": "folder_symbol_mismatch",
                            "detail": (
                                f"{len(dl)} file(s) under raw/trades/{sym}/ carry DBN query "
                                f"symbol {'|'.join(qs)}, i.e. they hold {root} data, not {sym}. "
                                f"{len(dupes)} of them duplicate the {root}/ file of the same date "
                                "(identical DBN metadata). Reading these as "
                                f"{sym} silently substitutes {root} prices; {sym} has no data at "
                                "all on those dates."
                            ),
                            "first_date": min(dl).isoformat(),
                            "last_date": max(dl).isoformat(),
                            "file_count": len(dl),
                            "dates": [d.isoformat() for d in sorted(dl)],
                            "duplicate_of_sibling_folder": dupes,
                        }
                    )

        profiles = {
            k: scache.get(k, size_by_key[k])
            for k in profile_keys
            if k.startswith(prefix + "/") and scache.get(k, size_by_key[k])
        }
        counts_here = {
            k: ccache.get(k, size_by_key[k])
            for k in sample_keys
            if k.startswith(prefix + "/") and ccache.get(k, size_by_key[k])
        }
        bad_width = [k for k, v in counts_here.items() if v["fixed_width_remainder"]]
        if bad_width:
            anomalies.append(
                {
                    "severity": "warning",
                    "prefix": prefix,
                    "kind": "variable_width_records",
                    "detail": f"{len(bad_width)} sampled file(s) are not an exact multiple of the "
                    "record width; counts for those are approximate",
                }
            )

        layers = sorted({r["meta"]["compression_layers"] for r in rows})
        layer_spans = {}
        for n in layers:
            ds = sorted(r["file_date"] for r in rows if r["meta"]["compression_layers"] == n)
            layer_spans[str(n)] = {
                "files": len(ds),
                "first_date": ds[0].isoformat(),
                "last_date": ds[-1].isoformat(),
            }
        if layers != [1]:
            spans = "; ".join(
                f"{v['files']} file(s) at {k} layer(s) ({v['first_date']} → {v['last_date']})"
                for k, v in sorted(layer_spans.items())
            )
            anomalies.append(
                {
                    "severity": "warning",
                    "prefix": prefix,
                    "kind": "inconsistent_zstd_layering",
                    "detail": "compression depth is not uniform across this prefix: "
                    + spans
                    + ". A reader that decompresses a fixed number of times fails on one "
                    "group or the other; unwrap until the DBN magic appears.",
                    "first_date": min(v["first_date"] for v in layer_spans.values()),
                    "last_date": max(v["last_date"] for v in layer_spans.values()),
                }
            )

        for sym, v in sorted(sym_entries.items()):
            hard = [
                d
                for d, why in v["missing_file_dates_classified"].items()
                if why != "evening_open_only_date"
            ]
            if hard:
                anomalies.append(
                    {
                        "severity": "warning",
                        "prefix": prefix,
                        "kind": "missing_session_date",
                        "detail": f"{sym}: {len(hard)} trading session date(s) have no file at "
                        f"all: {', '.join(hard)}. Classified from the {cal_name} calendar, so "
                        "these are not holidays.",
                        "first_date": min(hard),
                        "last_date": max(hard),
                    }
                )
            if v["files_with_foreign_content"]["count"]:
                anomalies.append(
                    {
                        "severity": "critical",
                        "prefix": prefix,
                        "kind": "silent_coverage_hole",
                        "detail": f"{sym} has no usable data on "
                        f"{v['files_with_foreign_content']['count']} date(s) even though a file "
                        "exists for each of them: "
                        f"{', '.join(v['files_with_foreign_content']['dates'])}",
                        "first_date": v["files_with_foreign_content"]["dates"][0],
                        "last_date": v["files_with_foreign_content"]["dates"][-1],
                    }
                )

        total_bytes = sum(r["size"] for r in rows)
        sampled_bytes = sum(size_by_key[k] for k in counts_here)
        sampled_rows = sum(v["records"] for v in counts_here.values())
        per_byte = sampled_rows / sampled_bytes if sampled_bytes else None

        prefixes[prefix] = {
            "objects": len(rows),
            "total_bytes": total_bytes,
            "file_format": "Databento DBN, zstd-compressed",
            "dbn_versions": sorted({r["meta"]["dbn_version"] for r in rows}),
            "compression_layers": layers,
            "compression_layer_spans": layer_spans,
            "record_size_bytes": sorted({r["meta"]["record_size"] for r in rows}),
            "dataset": sorted({r["meta"]["dataset"] for r in rows}),
            "dbn_schema": sorted({r["meta"]["schema"] for r in rows}),
            "stype_in": sorted({r["meta"]["stype_in"] for r in rows}),
            "stype_out": sorted({r["meta"]["stype_out"] for r in rows}),
            "ts_out": sorted({r["meta"]["ts_out"] for r in rows}),
            "partition": {
                "layout": (
                    "raw/trades/<SYMBOL_FOLDER>/glbx-mdp3-<YYYYMMDD>.trades.dbn.zst"
                    if prefix == "raw/trades"
                    else f"{prefix}/opra-pillar-<YYYYMMDD>.{rows[0]['kind']}.dbn.zst"
                ),
                "keys": ["symbol_folder", "utc_date"] if prefix == "raw/trades" else ["utc_date"],
                "one_object_per": "symbol x UTC date" if prefix == "raw/trades" else "UTC date",
                "date_basis": "UTC calendar day (from the DBN metadata start/end, "
                "which are UTC midnight boundaries) -- NOT the CME session date",
                "distinct_header_variants": len(header_variants),
            },
            "calendar": {"name": cal_name, **cal_info},
            "instrument_count_per_file": {
                "min": min(r["meta"]["instrument_count"] for r in rows),
                "median": int(statistics.median(r["meta"]["instrument_count"] for r in rows)),
                "max": max(r["meta"]["instrument_count"] for r in rows),
            },
            "row_counts": {
                "sampled_files": len(counts_here),
                "sampled_bytes": sampled_bytes,
                "sampled_rows_exact": sampled_rows,
                "rows_per_compressed_byte": round(per_byte, 6) if per_byte else None,
                "estimated_total_rows": int(round(per_byte * total_bytes)) if per_byte else None,
                "method": "exact count on a strided sample, extrapolated by compressed bytes; "
                "no prefix is scanned in full",
                "sampled_keys": sorted(counts_here),
            },
            "size_bytes": size_stats([r["size"] for r in rows]),
            "symbols": sym_entries,
            "sampled_profiles": profiles,
        }

    # ---- sidecar (non-DBN) objects ---------------------------------------- #
    sidecars = []
    for o in unparsed:
        sidecars.append({"key": o["key"], "size": o["size"]})
        if o["key"].endswith("condition.json"):
            body = s3.get_object(Bucket=bucket, Key=o["key"])["Body"].read()
            cond = json.loads(body)
            by_cond = Counter(c["condition"] for c in cond)
            sidecars[-1]["conditions"] = dict(sorted(by_cond.items()))
            sidecars[-1]["date_range"] = [cond[0]["date"], cond[-1]["date"]]
            sidecars[-1]["not_available"] = sorted(
                c["date"] for c in cond if c["condition"] != "available"
            )
            pfx = "raw/" + o["key"].split("/")[1]
            last_file = max(
                v["last_file_date"] for v in prefixes[pfx]["symbols"].values()
            )
            if cond[-1]["date"] < last_file:
                anomalies.append(
                    {
                        "severity": "warning",
                        "prefix": pfx,
                        "kind": "sidecar_coverage_short",
                        "detail": f"{o['key']} documents data condition only to "
                        f"{cond[-1]['date']}, but the prefix holds files through "
                        f"{last_file}. The later files come from a separate vendor job "
                        "and have no condition record; note this lines up with the "
                        "change in compression depth.",
                        "first_date": cond[-1]["date"],
                        "last_date": last_file,
                    }
                )
            for c in cond:
                if c["condition"] != "available":
                    anomalies.append(
                        {
                            "severity": "warning",
                            "prefix": "raw/opra_definition",
                            "kind": "vendor_condition",
                            "detail": f"Databento reports {c['date']} as '{c['condition']}'",
                            "first_date": c["date"],
                            "last_date": c["date"],
                        }
                    )

    # ---- cross-prefix coverage -------------------------------------------- #
    trades_syms = prefixes.get("raw/trades", {}).get("symbols", {})
    opra_first = min(
        (v["first_file_date"] for p, d in prefixes.items() if p != "raw/trades"
         for v in d["symbols"].values()),
        default=None,
    )
    opra_last = max(
        (v["last_file_date"] for p, d in prefixes.items() if p != "raw/trades"
         for v in d["symbols"].values()),
        default=None,
    )
    overlaps = {}
    for sym, v in sorted(trades_syms.items()):
        if opra_first and opra_last:
            lo, hi = max(v["first_file_date"], opra_first), min(v["last_file_date"], opra_last)
            overlaps[sym] = {"start": lo, "end": hi, "nonempty": lo <= hi}

    return {
        "schema_version": "1",
        "bucket": bucket,
        "generated_by": "scripts/inventory.py",
        "object_count": len(objects),
        "total_bytes": sum(o["size"] for o in objects),
        "prefixes": prefixes,
        "sidecar_objects": sorted(sidecars, key=lambda r: r["key"]),
        "coverage_overlap_trades_vs_opra": overlaps,
        "anomalies": sorted(
            anomalies, key=lambda a: (a["severity"], a["prefix"], a["kind"], a.get("first_date", ""))
        ),
        "notes": {
            "session_convention": f"CME Globex session opens {SESSION_OPEN_HOUR}:00 {CME_TZ} "
            "and the session date rolls there; files are split on UTC day, so one "
            "session spans two files.",
            "row_count_caveat": "Prefix row totals are extrapolations from exactly counted "
            "sampled files. Only sampled_rows_exact is a measured number.",
            "no_signal_content": "This inventory deliberately contains no prices, returns or "
            "any other outcome-derived statistic.",
        },
    }


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def human_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return ""


def render_markdown(m: dict) -> str:
    L: list[str] = []
    a = L.append
    a("# Data Inventory")
    a("")
    a(f"Generated by `scripts/inventory.py` from R2 bucket `{m['bucket']}`. "
      f"{m['object_count']:,} objects, {human_bytes(m['total_bytes'])}.")
    a("")
    a("Everything below is read from the bucket, not assumed. Re-run the script to "
      "regenerate; `data/manifest.json` holds the same content in full detail.")
    a("")

    # headline
    a("## Read this first")
    a("")
    tr = m["prefixes"].get("raw/trades", {}).get("symbols", {})
    a("Coverage, one line per symbol:")
    a("")
    for _p, d in sorted(m["prefixes"].items()):
        for sym, v in sorted(d["symbols"].items()):
            a(f"- **`{_p}` / {sym}** — {v['one_line_coverage']}")
    a("")
    crit = [x for x in m["anomalies"] if x["severity"] == "critical"]
    if crit:
        a("Things that will silently corrupt results if not handled:")
        a("")
        seen = set()
        for x in crit:
            if x["detail"] in seen:
                continue
            seen.add(x["detail"])
            a(f"- **{x['kind']}** — {x['detail']} ({x['first_date']} → {x['last_date']}).")
        a("")
    a("Two structural facts that contradict the task brief's starting assumptions:")
    a("")
    a("- The objects are **not parquet**. They are Databento DBN under zstd, so there is no "
      "parquet footer to read and `read_parquet` will not open them. Row counts and schema "
      "come from the DBN header plus a sampled decode instead.")
    a("- **ES is present**, with its own year of history, and the OPRA prefixes are QQQ "
      "options as described.")
    a("")

    # 1 partition
    a("## 1. Partition scheme")
    a("")
    a("| Prefix | Layout | One object per | Objects | Size |")
    a("|---|---|---|---|---|")
    for p, d in sorted(m["prefixes"].items()):
        a(f"| `{p}` | `{d['partition']['layout']}` | {d['partition']['one_object_per']} | "
          f"{d['objects']:,} | {human_bytes(d['total_bytes'])} |")
    a("")
    a("**The date in every filename is a UTC calendar day, not a CME session date.** "
      "Confirmed from each file's DBN metadata `start`/`end`, which sit on UTC midnight. "
      "The CME session runs 17:00 CT the previous evening to 16:00 CT, so a session's "
      "data is split across two consecutive files, and the bucket contains Sunday-dated "
      "files holding only the weekly 17:00 CT open up to UTC midnight. Any loader that "
      "treats one file as one session is wrong.")
    a("")

    # 2 symbols
    a("## 2. Symbols present in `trades`")
    a("")
    a("| Folder | Files | DBN query symbol(s) in the files | Dates |")
    a("|---|---|---|---|")
    for sym, v in sorted(tr.items()):
        for qs, info in sorted(v["dbn_query_symbols"].items()):
            a(f"| `{sym}` | {info['files']} | `{qs}` | {info['first_date']} → {info['last_date']} |")
    a("")
    if crit:
        a("> The folder name and the file contents disagree — see *Read this first*. "
          "The symbol column above comes from each file's own DBN header, which is "
          "authoritative; the folder name is not.")
        a("")

    # 3 coverage
    a("## 3. Date coverage")
    a("")
    a("| Prefix / symbol | First | Last | Files | Expected | Absent | Usable dates | No usable data |")
    a("|---|---|---|---|---|---|---|---|")
    for p, d in sorted(m["prefixes"].items()):
        for sym, v in sorted(d["symbols"].items()):
            label = f"`{p}` / {sym}" if p == "raw/trades" else f"`{p}`"
            a(f"| {label} | {v['first_file_date']} | {v['last_file_date']} | "
              f"{v['objects']} | {v['expected_file_dates']} | {v['missing_count']} | "
              f"{v['usable_file_dates']} | {v['content_missing_count']} |")
    a("")
    a("*Files* counts objects. *Usable dates* excludes files whose DBN header names a "
      "different instrument root than their folder, which is why the two columns differ "
      "for NQ. *No usable data* is the column that matters for a walk-forward split.")
    a("")
    ov = m["coverage_overlap_trades_vs_opra"]
    if ov:
        a("Overlap with the QQQ options data (the GEX feature source), by file-date "
          "range — subtract the unusable dates listed above before using it:")
        a("")
        for sym, o in sorted(ov.items()):
            a(f"- **{sym}**: {o['start']} → {o['end']}")
        a("")

    # 4 gaps
    a("## 4. Gaps")
    a("")
    a("Expected dates are derived from a trading calendar, not from the data. For "
      "`trades` the expectation is `{session date} ∪ {session date − 1 day}`, because "
      "of the UTC-split described above; for the OPRA prefixes it is the options "
      "trading calendar. Full holidays are therefore never counted as "
      "gaps; a date the calendar keeps as a shortened session is expected, and its "
      "absence is reported.")
    a("")
    for p, d in sorted(m["prefixes"].items()):
        for sym, v in sorted(d["symbols"].items()):
            label = f"`{p}` / {sym}" if p == "raw/trades" else f"`{p}`"
            a(f"**{label}** — calendar `{d['calendar']['name']}`, "
              f"{v['missing_count']} missing, {len(v['unexpected_file_dates'])} unexpected.")
            a("")
            if v["missing_file_dates"]:
                for d_, why in sorted(v["missing_file_dates_classified"].items()):
                    extra = ""
                    if why == "half_session_date":
                        extra = (
                            f" — but {v['half_session_dates_present']} of the "
                            f"{v['half_session_dates_in_range']} shortened sessions in range "
                            "do have a file, so this is a vendor gap rather than a "
                            "calendar artefact"
                        )
                    a(f"- **no file**: {d_} ({why.replace('_', ' ')}){extra}")
            else:
                a("- no missing files")
            if v["files_with_foreign_content"]["count"]:
                a("- **file present but holds another instrument's data**: "
                  + ", ".join(v["files_with_foreign_content"]["dates"]))
            if v["unexpected_file_dates"]:
                a("- present but not on the calendar: " + ", ".join(v["unexpected_file_dates"]))
            a("")
        if d["calendar"]["early_close_days"]:
            a(f"- `{p}` early closes (normal close {d['calendar']['normal_close_ct']} CT): "
              + ", ".join(d["calendar"]["early_close_days"]))
            a("")

    # 5 rows
    a("## 5. Row counts")
    a("")
    a("Exact counts come from decompressing a strided sample of files end to end and "
      "dividing by the fixed DBN record width; prefix totals extrapolate that by "
      "compressed bytes. No prefix is scanned in full — `raw/opra_cbbo1m` is 38 GiB.")
    a("")
    a("| Prefix / symbol | Record width | Sampled files | Rows counted | Estimated total rows |")
    a("|---|---|---|---|---|")
    for p, d in sorted(m["prefixes"].items()):
        rc = d["row_counts"]
        w = "/".join(str(x) for x in d["record_size_bytes"])
        est = f"{rc['estimated_total_rows']:,}" if rc["estimated_total_rows"] else "—"
        a(f"| `{p}` | {w} B | {rc['sampled_files']} | {rc['sampled_rows_exact']:,} | ~{est} |")
        if p == "raw/trades":
            for sym, v in sorted(d["symbols"].items()):
                r2 = v["row_counts"]
                e2 = f"{r2['estimated_total_rows']:,}" if r2["estimated_total_rows"] else "—"
                a(f"| `{p}` / {sym} | {w} B | {r2['sampled_files']} | "
                  f"{r2['sampled_rows_exact']:,} | ~{e2} |")
    a("")

    # 6 schema
    a("## 6. Schema and timestamps")
    a("")
    for p, d in sorted(m["prefixes"].items()):
        a(f"### `{p}`")
        a("")
        a(f"- dataset `{'/'.join(d['dataset'])}`, DBN schema `{'/'.join(d['dbn_schema'])}`, "
          f"DBN v{'/'.join(str(x) for x in d['dbn_versions'])}, "
          f"fixed record width {'/'.join(str(x) for x in d['record_size_bytes'])} bytes")
        a(f"- symbology: `stype_in={'/'.join(d['stype_in'])}` → "
          f"`stype_out={'/'.join(d['stype_out'])}`; `ts_out={'/'.join(str(x) for x in d['ts_out'])}`")
        if d["compression_layers"] == [1]:
            a("- zstd layers: 1")
        else:
            spans = "; ".join(
                f"**{k}×** for {v['files']} file(s) {v['first_date']} → {v['last_date']}"
                for k, v in sorted(d["compression_layer_spans"].items())
            )
            a(f"- zstd layers: **mixed** — {spans}. Unwrap in a loop until `DBN` magic appears.")
        a(f"- instruments referenced per file: min {d['instrument_count_per_file']['min']:,}, "
          f"median {d['instrument_count_per_file']['median']:,}, "
          f"max {d['instrument_count_per_file']['max']:,}")
        a(f"- distinct header variants across all {d['objects']:,} files: "
          f"{d['partition']['distinct_header_variants']}")
        prof = d["sampled_profiles"]
        if prof:
            k0 = sorted(prof)[0]
            sch = prof[k0]["schema"]
            a("")
            src = "first records of" if prof[k0].get("partial_read") else "a full read of"
            a(f"Columns (from {src} `{k0}`):")
            a("")
            a("| Column | dtype | unit | tz |")
            a("|---|---|---|---|")
            i = sch["index"]
            a(f"| _{i['name']}_ (index) | {i['dtype']} | {i.get('unit','—')} | {i.get('tz','—')} |")
            for info in sch["columns"]:
                a(f"| {info['name']} | {info['dtype']} | {info.get('unit','—')} "
                  f"| {info.get('tz','—')} |")
        else:
            a("")
            a("_No decoded sample for this prefix._")
        a("")
    a("**Timezone handling.** Every timestamp decodes as `datetime64[ns, UTC]` — "
      "nanosecond precision, UTC, never naive and never local. `ts_recv` is the "
      "DataFrame index (gateway receive time); `ts_event` is a separate column "
      "(exchange event time). Converting to CME session terms is a deliberate "
      f"`tz_convert('{CME_TZ}')` step, which handles the CDT/CST shift — do not "
      "apply a fixed UTC offset.")
    a("")

    # 7 session
    a("## 7. Session coverage in `trades`")
    a("")
    a("Determined from the CT timestamp distribution of sampled files, one per file "
      "shape per symbol:")
    a("")
    tp = m["prefixes"].get("raw/trades", {}).get("sampled_profiles", {})
    a("| Sampled file | Shape | Rows | CT span | 17:00 open | RTH |")
    a("|---|---|---|---|---|---|")
    for k in sorted(tp):
        pr = tp[k]
        if not pr.get("ct_hour_histogram"):
            continue
        parsed_key = parse_key(k)
        d = parsed_key["file_date"]
        a(f"| `{k.split('/')[-1]}` ({parsed_key['folder_symbol']}) "
          f"| {weekday_class(d).replace('_', ' ')} "
          f"| {pr['rows']:,} | {pr['ts_first_ct'][:19]} → {pr['ts_last_ct'][:19]} "
          f"| {'yes' if pr['covers_globex_open_1700ct'] else 'no'} "
          f"| {'yes' if pr['covers_rth_0830_1500ct'] else 'no'} |")
    a("")
    a("The sample deliberately covers all four shapes a UTC-day file takes:")
    a("")
    a("- **midweek** — a full 24 UTC hours, i.e. 19:00 CT the previous day → 18:59 CT "
      "under CDT and 18:00 → 17:59 CT under CST. Contains the tail of one session "
      "(through the 15:00 CT equity close and 16:00 CT futures close), the "
      "16:00–17:00 CT halt as an empty hour, and the *start of the next session* from "
      "17:00 CT.")
    a("- **friday** — same window, but Globex shuts for the week at 16:00 CT and does "
      "not reopen, so there is no 17:00 CT hour. A missing evening here is correct, "
      "not a gap.")
    a("- **sunday** — only the 17:00 CT weekly open through the UTC midnight boundary: "
      "two hours under CDT, one under CST, giving a file two orders of magnitude "
      "smaller than a weekday's.")
    a("")
    a("So: **full Globex, not RTH** — every session hour is present, including the "
      "overnight. But no file is a session. Bar building must concatenate consecutive "
      "files and cut on the 17:00 CT boundary; anchoring VWAP or session highs to a "
      "file boundary would anchor them to 18:00/19:00 CT, an hour or two into the "
      "session, and would shift by an hour at each DST change.")
    a("")

    # 8 sizes
    a("## 8. File-size distribution and outliers")
    a("")
    a("| Prefix / symbol | min | p05 | median | p95 | max | outliers |")
    a("|---|---|---|---|---|---|---|")
    for p, d in sorted(m["prefixes"].items()):
        for sym, v in sorted(d["symbols"].items()):
            s = v["size_bytes"]
            label = f"`{p}` / {sym}" if p == "raw/trades" else f"`{p}`"
            a(f"| {label} | {human_bytes(s['min'])} | {human_bytes(s['p05'])} | "
              f"{human_bytes(s['median'])} | {human_bytes(s['p95'])} | "
              f"{human_bytes(s['max'])} | {len(v['size_outliers'])} |")
    a("")
    a("Outlier rule: ≤0.10× or ≥4× the median of the 10 nearest files by date — a local "
      "comparison, so a uniformly small stretch is not flagged wholesale and a single "
      "truncated file inside one is. `explained_by` marks the structurally small files: "
      "`evening_open_only` is a date with no session of its own that exists solely to hold "
      "the 17:00 CT open of the next one (every Sunday, every holiday eve).")
    a("")
    a("| Prefix / symbol | outliers | evening open only | early close | unexplained |")
    a("|---|---|---|---|---|")
    for p, d in sorted(m["prefixes"].items()):
        for sym, v in sorted(d["symbols"].items()):
            o = v["size_outliers"]
            label = f"`{p}` / {sym}" if p == "raw/trades" else f"`{p}`"
            a(f"| {label} | {len(o)} "
              f"| {sum(1 for x in o if x['explained_by'] == 'evening_open_only')} "
              f"| {sum(1 for x in o if x['explained_by'] == 'early_close')} "
              f"| {sum(1 for x in o if not x['explained_by'])} |")
    a("")
    for p, d in sorted(m["prefixes"].items()):
        for sym, v in sorted(d["symbols"].items()):
            unexplained = [o for o in v["size_outliers"] if not o["explained_by"]]
            if not unexplained:
                continue
            label = f"{p}/{sym}" if p == "raw/trades" else p
            a(f"**{label}** — {len(unexplained)} unexplained:")
            a("")
            for o in unexplained[:25]:
                a(f"- {o['date']} — {human_bytes(o['size_bytes'])} vs neighbour median "
                  f"{human_bytes(o['neighbour_median_bytes'])} "
                  f"({o['ratio_to_neighbour_median']}×, {o['flag']})")
            if len(unexplained) > 25:
                a(f"- … {len(unexplained) - 25} more, see `data/manifest.json`")
            a("")

    # anomalies
    a("## Anomalies and design inputs")
    a("")
    for x in m["anomalies"]:
        rng = ""
        if x.get("first_date"):
            rng = f" [{x['first_date']}" + (f" → {x['last_date']}]" if x.get("last_date") else "]")
        a(f"- **{x['severity']}** `{x['prefix']}` *{x['kind']}* — {x['detail']}{rng}")
    a("")

    # sidecars
    a("## Vendor sidecar files")
    a("")
    for s in m["sidecar_objects"]:
        extra = ""
        if "conditions" in s:
            extra = (f" — conditions {s['conditions']}, covering {s['date_range'][0]} → "
                     f"{s['date_range'][1]}")
            if s["not_available"]:
                extra += f"; not fully available: {', '.join(s['not_available'])}"
        a(f"- `{s['key']}` ({human_bytes(s['size'])}){extra}")
    a("")
    a("---")
    a("")
    a("_No prices, returns or outcome statistics are computed here. The train/"
      "validation/holdout split is Task 2 and must be decided from the coverage "
      "numbers above._")
    a("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="re-probe every object, ignoring cache")
    ap.add_argument("--no-cache", action="store_true", help="neither read nor write the cache")
    ap.add_argument("--workers", type=int, default=24, help="parallel ranged reads (default 24)")
    args = ap.parse_args()

    manifest = build(args)

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    serialised = json.dumps(manifest, indent=2, sort_keys=True)
    MANIFEST_PATH.write_text(serialised + "\n")
    # Render from the round-tripped manifest: the document is then a pure function
    # of the committed JSON, not of whatever order this process happened to build.
    DOC_PATH.write_text(render_markdown(json.loads(serialised)))
    log(f"wrote {MANIFEST_PATH.relative_to(REPO_ROOT)} and {DOC_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
