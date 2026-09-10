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
import hashlib
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
import pandas as pd
import pandas_market_calendars as mcal
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The DBN reading rules -- double zstd, never seek by fixed offset, symbol from
# the header -- live in one place so this script and the NQ adapter cannot drift
# apart on them.
from src.core.data.dbn import (  # noqa: E402
    count_records,
    dbn_metadata,
    read_full_df,
    read_head_df,
    sha256_of,
    unwrap_zstd,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "data" / "manifest.json"
CORRECTIONS_PATH = REPO_ROOT / "config" / "corrections.yaml"
DOC_PATH = REPO_ROOT / "docs" / "data-inventory.md"
# Gitignored probe cache. Purely a speed-up -- deleting it changes nothing about
# the output, only how long a run takes. Lives under data/cache/ so the repo has
# one cache location rather than one per script.
CACHE_DIR = REPO_ROOT / "data" / "cache" / "inventory"

# Bump the relevant entry when a probe function changes shape or fixes a bug, so
# stale cache entries from an older version of this script are discarded rather
# than reused. Versions are per-cache so one change does not invalidate the rest.
CACHE_VERSIONS = {
    "dbn_metadata": 2,
    "record_counts": 2,
    "frame_profiles": 3,
    "checksums": 1,
}

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
    r"^(?P<root>raw)/(?P<group>[a-z0-9_]+)/"
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
    data/cache/ changes nothing about the output, only how long a run takes."""

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
# --------------------------------------------------------------------------- #
# corrections record
# --------------------------------------------------------------------------- #
def yaml_plain(x):
    """YAML turns bare dates into date objects and folds long scalars over several
    lines; both need flattening before they reach JSON."""
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    if isinstance(x, str):
        return " ".join(x.split())
    if isinstance(x, list):
        return [yaml_plain(i) for i in x]
    if isinstance(x, dict):
        return {str(k): yaml_plain(v) for k, v in x.items()}
    return x


def load_corrections(path: Path = CORRECTIONS_PATH) -> dict:
    """Read config/corrections.yaml.

    This is a historical record, not a mechanism: the bucket holds one canonical
    object per (symbol, date) and nothing here resolves a path or redirects a
    read. The inventory uses it for two narrow purposes -- reporting the recorded
    sha256 of the six corrected objects, and telling an expected divergence from
    an unexplained one when the integrity check finds a changed object.

    An absent file is not an error; a checkout with no corrections yet is a
    legitimate state and the inventory should still run.
    """
    if not path.exists():
        return {"corrections": [], "job_provenance": {}, "divergence": {}}
    doc = yaml_plain(yaml.safe_load(path.read_text()) or {})
    return {
        "corrections": sorted(
            doc.get("corrections") or [], key=lambda e: (e["symbol"], e["date"])
        ),
        "job_provenance": doc.get("job_provenance") or {},
        "divergence": doc.get("divergence_from_source_lake") or {},
    }


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
        "prefix": f"{d['root']}/{d['group']}",
        "root": d["root"],
        "folder_symbol": d["symbol"],
        "file_date": dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:])),
        "stem": d["stem"],
        "kind": d["kind"],
    }


def list_objects(s3, bucket: str) -> list[dict]:
    """One listing gives key, size, ETag and last-modified for every object.

    The ETag is what makes per-object integrity affordable: R2 returns the plain
    MD5 for a single-part upload, so the whole bucket can be fingerprinted from
    listings alone, at zero egress. A composite ETag (one containing a dash) is a
    multipart upload whose value is a hash of part hashes and is not comparable
    across re-uploads -- those are flagged rather than trusted.
    """
    out: list[dict] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            out.append(
                {
                    "key": o["Key"],
                    "size": o["Size"],
                    "etag": o["ETag"].strip('"'),
                    "last_modified": o["LastModified"]
                    .astimezone(dt.timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
    return sorted(out, key=lambda r: r["key"])


def previous_manifest(path: Path = MANIFEST_PATH) -> dict | None:
    """The committed manifest, used as the integrity baseline for this run."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


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


def assemblable_sessions(sessions: Iterable[dt.date], present: set[dt.date]) -> dict:
    """Which CME sessions can actually be reconstructed from the objects we hold.

    This is the denominator every power calculation needs, and it is not the file
    count. A session `D` runs 17:00 CT on `D-1` to 16:00 CT on `D`; in UTC that
    straddles exactly two daily objects, so `D` is usable only when both the
    `D-1` file (holding the evening open) and the `D` file are present.

    The gap between the two numbers is mostly a change of unit rather than lost
    data: Sunday and holiday-eve objects exist to carry an evening open and are
    inputs to a session, not sessions themselves.
    """
    sess = sorted(sessions)
    ok, no_own, no_prior = [], [], []
    for d in sess:
        if d not in present:
            no_own.append(d)
        elif (d - dt.timedelta(days=1)) not in present:
            no_prior.append(d)
        else:
            ok.append(d)
    return {
        "sessions": ok,
        "missing_own_file": no_own,
        "missing_prior_evening_file": no_prior,
        "evening_only_file_dates": sorted(present - set(sess)),
    }


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

    corrections = load_corrections()
    log(f"loaded {len(corrections['corrections'])} historical correction(s) from "
        f"{CORRECTIONS_PATH.relative_to(REPO_ROOT)}")
    baseline = previous_manifest()

    log("listing bucket ...")
    objects = list_objects(s3, bucket)

    parsed, unparsed = [], []
    for o in objects:
        p = parse_key(o["key"])
        if p:
            p.update(size=o["size"], etag=o["etag"], last_modified=o["last_modified"])
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
        if prefix.endswith("/trades"):
            for sym in sorted({r["folder_symbol"] for r in rows}):
                srows = [r for r in rows if r["folder_symbol"] == sym]
                sample_keys += [r["key"] for r in stride_sample(srows, SAMPLE_COUNTS[prefix])]
        else:
            sample_keys += [r["key"] for r in stride_sample(rows, SAMPLE_COUNTS.get(prefix, 4))]
    # The corrected objects are the ones whose identity is asserted in config, so
    # count them exactly rather than leaving it to the stride.
    sample_keys += [e["key"] for e in corrections["corrections"]]
    sample_keys = sorted({k for k in sample_keys if k in {r["key"] for r in parsed}})
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
        if prefix.endswith("/trades"):
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
                # and one file of each DBN version, so the v1/v3 mix inside raw/ is
                # evidenced rather than asserted
                for ver in sorted({r["meta"]["dbn_version"] for r in srows}):
                    pool = [
                        r
                        for r in srows
                        if r["meta"]["dbn_version"] == ver
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

    # ---- sha256 for the corrected objects ---------------------------------- #
    # Digests were computed when the replacements were made and are recorded in
    # config/corrections.yaml, so the default run copies them rather than pulling
    # the bytes again. --verify-sha256 re-reads those six objects (~18 MB) and
    # checks them; the live test tier does the same.
    hcache = Cache("checksums", enabled=not args.no_cache)
    corrected_keys = sorted({e["key"] for e in corrections["corrections"]} & set(size_by_key))
    if args.verify_sha256:
        pending = [
            k for k in corrected_keys if args.refresh or hcache.get(k, size_by_key[k]) is None
        ]
        if pending:
            log(f"verifying sha256 of {len(pending)} corrected object(s) ...")
            with ThreadPoolExecutor(min(6, args.workers)) as ex:
                hasher = lambda k: with_retry(sha256_of, s3, bucket, k)
                for key, digest in zip(pending, ex.map(hasher, pending)):
                    hcache.put(key, size_by_key[key], {"sha256": digest})
            hcache.flush()

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
            present = set(sdates)

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
            # not provide data for this symbol, however it is named.
            mismatched = [
                r
                for r in srows
                if prefix.endswith("/trades")
                and r["meta"]["query_symbols"]
                and r["meta"]["query_symbols"][0].split(".")[0] != sym
            ]

            versions = Counter(r["meta"]["dbn_version"] for r in srows)

            sym_entries[sym] = {
                "objects": len(srows),
                "total_bytes": total_bytes,
                "first_file_date": sdates[0].isoformat(),
                "last_file_date": sdates[-1].isoformat(),
                "distinct_file_dates": len(present),
                "dbn_versions": {
                    str(v): {
                        "files": n,
                        "first_date": min(
                            r["file_date"] for r in srows if r["meta"]["dbn_version"] == v
                        ).isoformat(),
                        "last_date": max(
                            r["file_date"] for r in srows if r["meta"]["dbn_version"] == v
                        ).isoformat(),
                    }
                    for v, n in sorted(versions.items())
                },
                "header_symbol_mismatches": {
                    "count": len(mismatched),
                    "dates": [r["file_date"].isoformat() for r in mismatched],
                    "keys": [r["key"] for r in mismatched],
                },
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

            # folder label vs actual content -- reported here as a storage fact;
            # whether it still costs us coverage is decided in the authority pass.
            if prefix.endswith("/trades"):
                meta_by_key = {r["key"]: r["meta"] for r in rows}
                for qs, dl in sorted(content_by_date.items()):
                    root = qs[0].split(".")[0] if qs else None
                    if not root or root == sym:
                        continue
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
                                f"{len(dl)} file(s) under {prefix}/{sym}/ carry DBN query "
                                f"symbol {'|'.join(qs)}, i.e. they hold {root} data, not {sym}. "
                                f"{len(dupes)} of them duplicate the {root}/ file of the same "
                                f"date (identical DBN metadata). Reading these as {sym} "
                                f"silently substitutes {root} prices."
                            ),
                            "first_date": min(dl).isoformat(),
                            "last_date": max(dl).isoformat(),
                            "file_count": len(dl),
                            "folder": sym,
                            "dates": [d.isoformat() for d in sorted(dl)],
                            "keys": [
                                f"{prefix}/{sym}/glbx-mdp3-{d:%Y%m%d}.trades.dbn.zst"
                                for d in sorted(dl)
                            ],
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
                # Derived from the keys themselves rather than hardcoded, so a new
                # prefix describes itself correctly the first time it appears.
                "layout": (
                    f"{prefix}/<SYMBOL_FOLDER>/{rows[0]['stem']}-<YYYYMMDD>."
                    f"{rows[0]['kind']}.dbn.zst"
                    if rows[0]["folder_symbol"]
                    else f"{prefix}/{rows[0]['stem']}-<YYYYMMDD>.{rows[0]['kind']}.dbn.zst"
                ),
                "keys": (
                    ["symbol_folder", "utc_date"] if rows[0]["folder_symbol"] else ["utc_date"]
                ),
                "one_object_per": (
                    "symbol x UTC date" if rows[0]["folder_symbol"] else "UTC date"
                ),
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

    # ---- integrity against the committed manifest -------------------------- #
    # The in-place overwrite gave up `rclone check` against the source lake as a
    # mirror-integrity test, so the repo has to be able to answer "did anything in
    # raw/ change?" on its own. Size and ETag come from the listing, which costs no
    # egress, and are compared against whatever data/manifest.json held before this
    # run. A change on an object named in config/corrections.yaml is the recorded
    # divergence and is reported as such; anything else is a critical anomaly.
    by_key = {r["key"]: r for r in parsed}
    corrected_by_key = {e["key"]: e for e in corrections["corrections"]}

    composite = sorted(o["key"] for o in objects if "-" in o["etag"])
    if composite:
        anomalies.append(
            {
                "severity": "warning",
                "prefix": "bucket",
                "kind": "composite_etag",
                "detail": f"{len(composite)} object(s) have a multipart ETag, which is a "
                "hash of part hashes rather than the object's MD5 and is not comparable "
                "across re-uploads: " + ", ".join(composite[:10])
                + (" ..." if len(composite) > 10 else "")
                + ". Integrity for those falls back to size alone.",
            }
        )

    integrity: dict[str, Any] = {
        "method": "size and ETag from ListObjectsV2, compared against the previous "
        "data/manifest.json. R2 returns the plain MD5 as the ETag for single-part "
        "uploads, so this fingerprints every object at zero egress.",
        "objects_fingerprinted": len(objects),
        "composite_etags": composite,
        "baseline": "data/manifest.json (previous revision)" if baseline else None,
        "comparable_baseline": False,
        "added": [],
        "removed": [],
        "changed": [],
        "status": "no baseline",
    }
    old_files = (baseline or {}).get("files") or {}
    old_fields = (baseline or {}).get("files_fields") or []
    if old_files:
        i_size = old_fields.index("size_bytes") if "size_bytes" in old_fields else 0
        i_etag = old_fields.index("etag") if "etag" in old_fields else None
        integrity["comparable_baseline"] = True
        integrity["baseline_had_etags"] = i_etag is not None
        live = {o["key"]: o for o in objects}
        integrity["added"] = sorted(set(live) - set(old_files))
        integrity["removed"] = sorted(set(old_files) - set(live))
        for key in sorted(set(live) & set(old_files)):
            row, cur = old_files[key], live[key]
            diffs = {}
            if row[i_size] != cur["size"]:
                diffs["size_bytes"] = [row[i_size], cur["size"]]
            if i_etag is not None and len(row) > i_etag and row[i_etag] != cur["etag"]:
                diffs["etag"] = [row[i_etag], cur["etag"]]
            if diffs:
                integrity["changed"].append(
                    {
                        "key": key,
                        "changes": diffs,
                        "classification": (
                            "recorded_correction" if key in corrected_by_key else "unexplained"
                        ),
                    }
                )
        unexplained = [c for c in integrity["changed"] if c["classification"] == "unexplained"]
        if unexplained or integrity["added"] or integrity["removed"]:
            # The comparison is against the manifest on disk, which this run is about
            # to overwrite. Say so loudly: the durable record of a drift is the git
            # diff of data/manifest.json, so commit or inspect before re-running.
            log(f"DRIFT: {len(unexplained)} unexplained change(s), "
                f"{len(integrity['added'])} added, {len(integrity['removed'])} removed "
                "vs the committed manifest. Inspect `git diff data/manifest.json` before "
                "re-running -- a second run re-baselines against this one.")
        integrity["status"] = (
            "clean"
            if not (unexplained or integrity["added"] or integrity["removed"])
            else "drift"
        )
        if unexplained:
            anomalies.append(
                {
                    "severity": "critical",
                    "prefix": "bucket",
                    "kind": "object_changed_unexplained",
                    "detail": f"{len(unexplained)} object(s) differ in size or ETag from the "
                    "committed manifest and are not named in config/corrections.yaml: "
                    + ", ".join(c["key"] for c in unexplained[:10])
                    + (" ..." if len(unexplained) > 10 else "")
                    + ". Either the bucket was modified without a record, or the record is "
                    "missing an entry.",
                }
            )
        for kind, keys, sev in (
            ("object_removed", integrity["removed"], "critical"),
            ("object_added", integrity["added"], "warning"),
        ):
            if keys:
                anomalies.append(
                    {
                        "severity": sev,
                        "prefix": "bucket",
                        "kind": kind,
                        "detail": f"{len(keys)} object(s) {kind.split('_')[1]} since the "
                        "committed manifest: " + ", ".join(keys[:10])
                        + (" ..." if len(keys) > 10 else ""),
                    }
                )

    # every object is expected to live under raw/; a second location reappearing
    # is exactly the situation this task removed, so say so rather than absorb it
    stray = sorted(o["key"] for o in objects if not o["key"].startswith("raw/"))
    if stray:
        anomalies.append(
            {
                "severity": "critical",
                "prefix": "bucket",
                "kind": "object_outside_raw",
                "detail": f"{len(stray)} object(s) sit outside raw/, which is meant to be the "
                "one canonical location: " + ", ".join(stray[:10])
                + (" ..." if len(stray) > 10 else ""),
            }
        )

    for e in corrections["corrections"]:
        if e["key"] not in by_key:
            anomalies.append(
                {
                    "severity": "critical",
                    "prefix": str(CORRECTIONS_PATH.relative_to(REPO_ROOT)),
                    "kind": "corrected_object_missing",
                    "detail": f"{e['date']} {e['symbol']}: {e['key']} is recorded as corrected "
                    "but no such object is in the bucket.",
                    "first_date": e["date"],
                    "last_date": e["date"],
                }
            )
            continue
        cur = by_key[e["key"]]
        want = e.get("replacement") or {}
        header = (cur["meta"]["query_symbols"] or [None])[0]
        if header != want.get("dbn_header_symbol") or cur["size"] != want.get("size_bytes"):
            anomalies.append(
                {
                    "severity": "critical",
                    "prefix": str(CORRECTIONS_PATH.relative_to(REPO_ROOT)),
                    "kind": "correction_reverted",
                    "detail": f"{e['key']} reports {header} at {cur['size']:,} bytes; "
                    f"corrections.yaml records the replacement as "
                    f"{want.get('dbn_header_symbol')} at {want.get('size_bytes'):,} bytes. "
                    "The corrected bytes may have been reverted.",
                    "first_date": e["date"],
                    "last_date": e["date"],
                }
            )
        recorded = hcache.get(e["key"], cur["size"]) if args.verify_sha256 else None
        if recorded and recorded["sha256"] != want.get("sha256"):
            anomalies.append(
                {
                    "severity": "critical",
                    "prefix": str(CORRECTIONS_PATH.relative_to(REPO_ROOT)),
                    "kind": "correction_checksum_mismatch",
                    "detail": f"{e['key']} hashes to {recorded['sha256']} but "
                    f"corrections.yaml declares {want.get('sha256')}.",
                    "first_date": e["date"],
                    "last_date": e["date"],
                }
            )

    # ---- coverage ----------------------------------------------------------- #
    # One canonical object per (symbol, date) under raw/, so coverage is a straight
    # read of the bucket. There is no second location and nothing to resolve.
    instrument_coverage: dict[str, Any] = {}
    session_sets: dict[str, set[dt.date]] = {}   # symbol -> assemblable session dates
    usable_file_dates: dict[str, set[dt.date]] = {}
    for prefix, rows in sorted(by_prefix.items()):
        cal_name = CALENDARS[prefix]
        for sym in sorted({r["folder_symbol"] or (r["meta"]["query_symbols"] or ["<none>"])[0]
                           for r in rows}):
            resolved = {
                r["file_date"]: r
                for r in sorted(rows, key=lambda r: r["file_date"])
                if (r["folder_symbol"] or (r["meta"]["query_symbols"] or ["<none>"])[0]) == sym
            }
            corrected_dates = sorted(
                d for d, r in resolved.items() if r["key"] in corrected_by_key
            )

            dates = sorted(resolved)
            sessions, cal_info = trading_days(cal_name, dates[0], dates[-1])
            session_set, early = set(sessions), set(cal_info["early_close_days"])
            if prefix.endswith("/trades"):
                expected = expected_utc_file_dates(
                    [d for d in sessions if dates[0] <= d <= dates[-1]]
                )
                expected = {d for d in expected if dates[0] <= d <= dates[-1]}
            else:
                expected = {d for d in sessions if dates[0] <= d <= dates[-1]}

            present = set(dates)
            # an object counts only if its own DBN header names this symbol
            usable = {
                d
                for d, r in resolved.items()
                if not prefix.endswith("/trades")
                or (
                    r["meta"]["query_symbols"]
                    and r["meta"]["query_symbols"][0].split(".")[0] == sym
                )
            }
            unusable = sorted(present - usable)
            absent = sorted(expected - present)
            missing = sorted(expected - usable)
            unexpected = sorted(present - expected)

            def classify(d: dt.date) -> str:
                if d not in session_set:
                    return "evening_open_only_date"
                if d.isoformat() in early:
                    return "half_session_date"
                return "full_session_date"

            half_in_range = [
                e_ for e_ in sorted(early) if dates[0].isoformat() <= e_ <= dates[-1].isoformat()
            ]
            half_present = sum(1 for e_ in half_in_range if dt.date.fromisoformat(e_) in usable)

            # The session-level view. For the OPRA prefixes a session needs only its
            # own file -- QQQ options trade 08:30-15:00 CT, inside one UTC day -- so
            # the two numbers coincide there and diverge only for futures.
            span_sessions = [d for d in sessions if dates[0] <= d <= dates[-1]]
            if prefix.endswith("/trades"):
                asm = assemblable_sessions(span_sessions, usable)
            else:
                asm = {
                    "sessions": [d for d in span_sessions if d in usable],
                    "missing_own_file": [d for d in span_sessions if d not in usable],
                    "missing_prior_evening_file": [],
                    "evening_only_file_dates": [],
                }

            session_sets[sym] = set(asm["sessions"])
            usable_file_dates[sym] = set(usable)

            sources = Counter(r["prefix"] for r in resolved.values())
            versions = Counter(r["meta"]["dbn_version"] for r in resolved.values())

            in_sample = [
                r
                for r in resolved.values()
                if r["key"] in sample_key_set and ccache.get(r["key"], r["size"])
            ]
            srows_ = sum(ccache.get(r["key"], r["size"])["records"] for r in in_sample)
            sbytes_ = sum(r["size"] for r in in_sample)
            tbytes_ = sum(r["size"] for r in resolved.values())
            per_byte = srows_ / sbytes_ if sbytes_ else None

            instrument_coverage[sym] = {
                "calendar": {"name": cal_name, **cal_info},
                "first_date": dates[0].isoformat(),
                "last_date": dates[-1].isoformat(),
                "file_dates_present": len(present),
                "expected_file_dates": len(expected),
                "usable_file_dates": len(usable),
                "missing_file_dates": [d.isoformat() for d in missing],
                "missing_count": len(missing),
                "missing_file_dates_classified": {d.isoformat(): classify(d) for d in missing},
                "absent_file_dates": [d.isoformat() for d in absent],
                "present_but_unusable_dates": [d.isoformat() for d in unusable],
                "unexpected_file_dates": [d.isoformat() for d in unexpected],
                "half_session_dates_in_range": len(half_in_range),
                "half_session_dates_present": half_present,
                "historically_corrected_dates": [d.isoformat() for d in corrected_dates],
                "sessions": {
                    "assemblable": len(asm["sessions"]),
                    # The list, not only the count. An adapter that re-derives it
                    # from a calendar is a second implementation of the rule, and
                    # the two will disagree on exactly the days that matter --
                    # Sundays and holiday eves are file dates but not sessions.
                    "dates": [d.isoformat() for d in asm["sessions"]],
                    "first": asm["sessions"][0].isoformat() if asm["sessions"] else None,
                    "last": asm["sessions"][-1].isoformat() if asm["sessions"] else None,
                    "candidate_sessions_in_span": len(span_sessions),
                    "missing_own_file": [d.isoformat() for d in asm["missing_own_file"]],
                    "missing_prior_evening_file": [
                        d.isoformat() for d in asm["missing_prior_evening_file"]
                    ],
                    "evening_only_file_dates": len(asm["evening_only_file_dates"]),
                    "definition": "a CME session D is assemblable when both UTC objects it "
                    "spans are present: the D-1 file holding the 17:00 CT evening open and "
                    "the D file holding the rest. File-date counts are NOT session counts.",
                },
                "sources": {k: v for k, v in sorted(sources.items())},
                "dbn_versions": {str(k): v for k, v in sorted(versions.items())},
                "total_bytes": tbytes_,
                "row_counts": {
                    "sampled_files": len(in_sample),
                    "sampled_bytes": sbytes_,
                    "sampled_rows_exact": srows_,
                    "rows_per_compressed_byte": round(per_byte, 6) if per_byte else None,
                    "estimated_total_rows": int(round(per_byte * tbytes_)) if per_byte else None,
                },
                "corrected_objects": {
                    d.isoformat(): {
                        "key": resolved[d]["key"],
                        "header_symbol": (resolved[d]["meta"]["query_symbols"] or [None])[0],
                        "dbn_version": resolved[d]["meta"]["dbn_version"],
                        "size_bytes": resolved[d]["size"],
                        "etag": resolved[d]["etag"],
                        "sha256": (corrected_by_key[resolved[d]["key"]].get("replacement") or {})
                        .get("sha256"),
                    }
                    for d in corrected_dates
                },
                "one_line_coverage": (
                    f"**{sym}**: {len(asm['sessions'])} assemblable sessions "
                    f"({asm['sessions'][0].isoformat() if asm['sessions'] else '-'} to "
                    f"{asm['sessions'][-1].isoformat() if asm['sessions'] else '-'}) "
                    f"from {len(usable)} usable file-dates, "
                    f"{len(missing)} expected file-date(s) with no usable data"
                    + (
                        f"; {len(corrected_dates)} date(s) hold historically corrected data"
                        if corrected_dates
                        else ""
                    )
                ),
            }

            hard = [
                d for d, why in instrument_coverage[sym]["missing_file_dates_classified"].items()
                if why != "evening_open_only_date"
            ]
            if hard:
                anomalies.append(
                    {
                        "severity": "warning",
                        "prefix": prefix,
                        "kind": "missing_session_date",
                        "detail": f"{sym}: {len(hard)} trading session date(s) have no usable "
                        f"data: {', '.join(hard)}. Classified from the {cal_name} calendar, "
                        "so these are not holidays.",
                        "first_date": min(hard),
                        "last_date": max(hard),
                    }
                )
            if unusable:
                anomalies.append(
                    {
                        "severity": "critical",
                        "prefix": prefix,
                        "kind": "unpatched_coverage_hole",
                        "detail": f"{sym} has no usable data on {len(unusable)} date(s) even "
                        "though a file exists and no supersession covers them: "
                        + ", ".join(d.isoformat() for d in unusable),
                        "first_date": unusable[0].isoformat(),
                        "last_date": unusable[-1].isoformat(),
                    }
                )

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
            parts = o["key"].split("/")
            pfx = "/".join(parts[:2])
            last_file = (
                max(v["last_file_date"] for v in prefixes[pfx]["symbols"].values())
                if pfx in prefixes
                else None
            )
            if last_file and cond[-1]["date"] < last_file:
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
                            "prefix": pfx,
                            "kind": "vendor_condition",
                            "detail": f"Databento reports {c['date']} as '{c['condition']}'",
                            "first_date": c["date"],
                            "last_date": c["date"],
                        }
                    )

    # ---- cross-instrument coverage ---------------------------------------- #
    # The number a GEX-using strategy is actually capped by: futures sessions that
    # also have same-day options data. Same-day is the right test -- QQQ options
    # trade 08:30-15:00 CT, inside the session's own UTC day, so unlike the futures
    # side there is nothing to assemble. A prior-session feature must look up the
    # previous *options trading day*, never D-1: every Monday's D-1 is a Sunday with
    # no OPRA file by construction, and that mistake silently deletes ~110 sessions.
    futures = {k: v for k, v in instrument_coverage.items() if "." not in k}
    options = {k: v for k, v in instrument_coverage.items() if "." in k}
    opra_first = min((v["first_date"] for v in options.values()), default=None)
    opra_last = max((v["last_date"] for v in options.values()), default=None)
    overlaps = {}
    for sym, v in sorted(futures.items()):
        if not (opra_first and opra_last):
            continue
        lo, hi = max(v["first_date"], opra_first), min(v["last_date"], opra_last)
        entry = {"file_date_range": {"start": lo, "end": hi, "nonempty": lo <= hi}}
        for opt, ov in sorted(options.items()):
            both = sorted(session_sets[sym] & usable_file_dates[opt])
            only_fut = sorted(session_sets[sym] - usable_file_dates[opt])
            entry[opt] = {
                "sessions_with_same_day_options": len(both),
                "first": both[0].isoformat() if both else None,
                "last": both[-1].isoformat() if both else None,
                "futures_sessions_without_options": [d.isoformat() for d in only_fut],
                "note": "futures sessions the options data cannot support; these are "
                "days CME equity futures trade but US equity options are closed",
            }
        overlaps[sym] = entry

    # ---- per-file record --------------------------------------------------- #
    # One compact row per DBN object. dbn_version in particular is permanently
    # heterogeneous (the mirror is v1, later downloads are v3+), so it belongs in
    # the manifest rather than being rediscovered at read time.
    # Every object, not just the DBN ones: the integrity comparison is meant to
    # cover the whole bucket, and the vendor JSON sidecars are as worth watching as
    # the data. Non-DBN objects carry nulls in the DBN columns.
    meta_by_key = {r["key"]: r for r in parsed}
    files = {}
    for o in sorted(objects, key=lambda o: o["key"]):
        r = meta_by_key.get(o["key"])
        files[o["key"]] = [
            o["size"],
            r["meta"]["dbn_version"] if r else None,
            r["meta"]["compression_layers"] if r else None,
            (r["meta"]["query_symbols"] or [None])[0] if r else None,
            o["etag"],
            o["last_modified"],
        ]

    return {
        "schema_version": "3",
        "bucket": bucket,
        "generated_by": "scripts/inventory.py",
        "object_count": len(objects),
        "total_bytes": sum(o["size"] for o in objects),
        "files_fields": [
            "size_bytes",
            "dbn_version",
            "zstd_layers",
            "dbn_header_symbol",
            "etag",
            "last_modified",
        ],
        "files": files,
        "integrity": integrity,
        "corrections": {
            "config": str(CORRECTIONS_PATH.relative_to(REPO_ROOT)),
            "count": len(corrections["corrections"]),
            "divergence_from_source_lake": corrections["divergence"],
            "entries": [
                {
                    **e,
                    "in_bucket": e["key"] in by_key,
                    "current_header_symbol": (
                        (by_key[e["key"]]["meta"]["query_symbols"] or [None])[0]
                        if e["key"] in by_key
                        else None
                    ),
                    "current_dbn_version": (
                        by_key[e["key"]]["meta"]["dbn_version"] if e["key"] in by_key else None
                    ),
                    "current_size_bytes": by_key[e["key"]]["size"] if e["key"] in by_key else None,
                    "current_etag": by_key[e["key"]]["etag"] if e["key"] in by_key else None,
                    "current_records_exact": (
                        (ccache.get(e["key"], by_key[e["key"]]["size"]) or {}).get("records")
                        if e["key"] in by_key
                        else None
                    ),
                    "sha256_source": str(CORRECTIONS_PATH.relative_to(REPO_ROOT)),
                    "sha256_verified_against_bucket": (
                        (
                            (hcache.get(e["key"], by_key[e["key"]]["size"]) or {}).get("sha256")
                            == (e.get("replacement") or {}).get("sha256")
                        )
                        if (args.verify_sha256 and e["key"] in by_key)
                        else None
                    ),
                }
                for e in corrections["corrections"]
            ],
            "job_provenance": corrections["job_provenance"],
        },
        "instrument_coverage": instrument_coverage,
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
            "integrity_baseline": "The integrity block compares the live listing against the "
            "data/manifest.json present before this run. Re-running re-baselines, so the "
            "durable record of a drift is the git diff of this file.",
            "canonical_location": "raw/ holds exactly one object per (symbol, date). Nothing "
            "resolves a path at read time; config/corrections.yaml is a historical record, "
            "not a mechanism.",
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
    cov = m["instrument_coverage"]
    corr = m["corrections"]
    integ = m["integrity"]
    a("`raw/` is the one canonical location: one object per (symbol, date), nothing to "
      "resolve at read time. Coverage, one line per instrument:")
    a("")
    for sym, v in sorted(cov.items()):
        a(f"- {v['one_line_coverage']}")
    a("")
    if corr["count"]:
        div = corr["divergence_from_source_lake"] or {}
        a(f"**`raw/` is not byte-identical to `{div.get('source', 'the source lake')}`.** "
          f"{corr['count']} object(s) were corrected in place on {div.get('applied')} after "
          "Task 1 found them holding another instrument's data; the contaminated originals "
          f"remain in `{div.get('originals_recoverable_from', div.get('source'))}`, so nothing "
          f"is unrecoverable. `{corr['config']}` is the record — evidence, not a mechanism: "
          "nothing reads it at runtime. See §9.")
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
            rng = (f" ({x['first_date']} → {x['last_date']})") if x.get("first_date") else ""
            a(f"- **{x['kind']}** — {x['detail']}{rng}")
        a("")
    a("Three structural facts worth knowing before writing a reader:")
    a("")
    a("- The objects are **not parquet**. They are Databento DBN under zstd, so there is no "
      "parquet footer to read and `read_parquet` will not open them. Row counts and schema "
      "come from the DBN header plus a sampled decode instead.")
    a("- **ES is present**, with its own year of history, and the OPRA prefixes are QQQ "
      "options as described.")
    a("- **DBN version is not uniform inside `raw/`.** Most of the mirror is v1; the six "
      "corrected objects are v3. Both decode to the same columns and dtypes, but the "
      "version is recorded per object in `data/manifest.json` under `files` rather than "
      "assumed. This mix is permanent.")
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
    a("| Prefix | Folder | Files | DBN query symbol(s) in the files | Dates |")
    a("|---|---|---|---|---|")
    for pfx, d in sorted(m["prefixes"].items()):
        if not pfx.endswith("/trades"):
            continue
        for sym, v in sorted(d["symbols"].items()):
            for qs, info in sorted(v["dbn_query_symbols"].items()):
                a(f"| `{pfx}` | `{sym}` | {info['files']} | `{qs}` "
                  f"| {info['first_date']} → {info['last_date']} |")
    a("")
    a("The symbol column comes from each object's own DBN header, which is "
      "authoritative; the folder name is not. The two must agree for every object — a "
      "file whose header names another instrument would be excluded from coverage and "
      f"raise a critical anomaly. Six objects once disagreed; see `{corr['config']}` "
      "and §9.")
    a("")

    # 3 coverage
    a("## 3. Date coverage")
    a("")
    a("**Assemblable sessions, not file-dates, is the denominator for any power "
      "calculation.** A CME session `D` runs 17:00 CT on `D-1` to 16:00 CT on `D`, so in "
      "UTC it straddles exactly two objects; `D` is usable only when both are present. "
      "Sunday and holiday-eve objects carry an evening open and are inputs to a session, "
      "not sessions in their own right, which is most of the difference between the two "
      "columns below.")
    a("")
    a("| Instrument | **Sessions** | Session span | Objects | Usable file-dates "
      "| File-date gaps | Corrected |")
    a("|---|---|---|---|---|---|---|")
    for sym, v in sorted(cov.items()):
        se = v["sessions"]
        a(f"| {sym} | **{se['assemblable']}** | {se['first']} → {se['last']} "
          f"| {sum(v['sources'].values())} | {v['usable_file_dates']} "
          f"| {v['missing_count']} | {len(v['historically_corrected_dates'])} |")
    a("")
    for sym, v in sorted(cov.items()):
        se = v["sessions"]
        lost = se["missing_own_file"] + se["missing_prior_evening_file"]
        if not lost and not se["evening_only_file_dates"]:
            continue
        bits = []
        if se["evening_only_file_dates"]:
            bits.append(f"{se['evening_only_file_dates']} evening-open-only objects")
        if se["missing_own_file"]:
            bits.append("no own file on " + ", ".join(se["missing_own_file"]))
        if se["missing_prior_evening_file"]:
            bits.append(
                "no prior-evening file for " + ", ".join(se["missing_prior_evening_file"])
            )
        a(f"- **{sym}**: {se['candidate_sessions_in_span']} candidate sessions in span; "
          + "; ".join(bits) + f" → {se['assemblable']} assemblable.")
    a("")
    a("*File-date gaps* counts expected dates with no usable data, from absent objects and "
      "from objects whose header names another instrument. A gap on an evening-only date "
      "costs the following session, not its own.")
    a("")
    ov = m["coverage_overlap_trades_vs_opra"]
    if ov:
        a("### Sessions a GEX-using strategy can actually use")
        a("")
        a("Futures sessions that also have **same-day** options data. Same-day is the "
          "correct test: QQQ options trade 08:30–15:00 CT, inside the session's own UTC "
          "day, so unlike the futures side there is nothing to assemble. A feature "
          "needing the *prior* session's options must look up the previous **options "
          "trading day** — never `D-1`, whose Monday value is a Sunday with no OPRA file "
          "by construction.")
        a("")
        a("| Futures | Options | Sessions with same-day options | Span | Futures sessions "
          "without |")
        a("|---|---|---|---|---|")
        for sym, o in sorted(ov.items()):
            for opt, x in sorted(o.items()):
                if opt == "file_date_range":
                    continue
                a(f"| {sym} | {opt} | **{x['sessions_with_same_day_options']}** "
                  f"| {x['first']} → {x['last']} "
                  f"| {len(x['futures_sessions_without_options'])} |")
        a("")
        for sym, o in sorted(ov.items()):
            for opt, x in sorted(o.items()):
                if opt == "file_date_range" or not x["futures_sessions_without_options"]:
                    continue
                a(f"{sym} sessions with no {opt} data — days CME equity futures trade but "
                  "US equity options are closed: "
                  + ", ".join(x["futures_sessions_without_options"]))
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
    for sym, v in sorted(cov.items()):
        a(f"**{sym}** — calendar `{v['calendar']['name']}`, {v['missing_count']} gap(s), "
          f"{len(v['unexpected_file_dates'])} unexpected.")
        a("")
        if v["missing_file_dates"]:
            for d_, why in sorted(v["missing_file_dates_classified"].items()):
                extra = ""
                if why == "half_session_date":
                    extra = (
                        f" — but {v['half_session_dates_present']} of the "
                        f"{v['half_session_dates_in_range']} shortened sessions in range "
                        "do have usable data, so this is a vendor gap rather than a "
                        "calendar artefact"
                    )
                kind = (
                    "no file"
                    if d_ in v["absent_file_dates"]
                    else "file present but unusable"
                )
                a(f"- **{kind}**: {d_} ({why.replace('_', ' ')}){extra}")
        else:
            a("- no gaps")
        if v["historically_corrected_dates"]:
            a("- historically corrected (see §9): "
              + ", ".join(v["historically_corrected_dates"]))
        if v["unexpected_file_dates"]:
            a("- present but not on the calendar: " + ", ".join(v["unexpected_file_dates"]))
        a("")
    for pfx, d in sorted(m["prefixes"].items()):
        if d["calendar"]["early_close_days"] and pfx.startswith("raw/"):
            a(f"- `{pfx}` early closes (normal close {d['calendar']['normal_close_ct']} CT): "
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
    def total_cell(rc: dict, objects: int) -> str:
        # When every object was counted there is nothing to extrapolate, and
        # calling the number an estimate would understate what we know.
        if not rc["estimated_total_rows"]:
            return "—"
        if rc["sampled_files"] >= objects:
            return f"{rc['sampled_rows_exact']:,} (exact, all files counted)"
        return f"~{rc['estimated_total_rows']:,}"

    for p, d in sorted(m["prefixes"].items()):
        rc = d["row_counts"]
        w = "/".join(str(x) for x in d["record_size_bytes"])
        a(f"| `{p}` | {w} B | {rc['sampled_files']} | {rc['sampled_rows_exact']:,} "
          f"| {total_cell(rc, d['objects'])} |")
        if p.endswith("/trades"):
            for sym, v in sorted(d["symbols"].items()):
                r2 = v["row_counts"]
                a(f"| `{p}` / {sym} | {w} B | {r2['sampled_files']} | "
                  f"{r2['sampled_rows_exact']:,} | {total_cell(r2, v['objects'])} |")
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
        vspans = []
        for sym, sv in sorted(d["symbols"].items()):
            for ver, info in sorted(sv["dbn_versions"].items()):
                vspans.append(f"v{ver}: {info['files']} file(s) in `{sym}` "
                              f"({info['first_date']} → {info['last_date']})")
        a("- DBN versions: " + "; ".join(vspans))
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
    by_ver: dict[int, list[str]] = {}
    for k in sorted(tp):
        by_ver.setdefault(m["files"][k][1], []).append(k)
    if len(by_ver) > 1:
        a("The DBN v3 objects behave identically to the v1 ones — same columns, same "
          "dtypes, same index (see §6), and the same session shape:")
        a("")
        for ver in sorted(by_ver):
            for k in by_ver[ver]:
                pr = tp[k]
                if not pr.get("ct_hour_histogram"):
                    continue
                pk = parse_key(k)
                a(f"- **v{ver}** `{k}` — {weekday_class(pk['file_date']).replace('_', ' ')}, "
                  f"{pr['rows']:,} rows, CT {pr['ts_first_ct'][:19]} → "
                  f"{pr['ts_last_ct'][:19]}")
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

    # 9 corrections
    a("## 9. Corrections and divergence from the source lake")
    a("")
    if not corr["count"]:
        a(f"None recorded in `{corr['config']}`; `raw/` matches the source lake.")
        a("")
    else:
        div = corr["divergence_from_source_lake"] or {}
        a(f"**`raw/` deliberately diverges from `{div.get('source')}` on "
          f"{div.get('objects_differing', corr['count'])} objects and on nothing else.**")
        a("")
        a("Task 1 found six objects under `raw/trades/NQ/` whose DBN header reported "
          "`ES.FUT`, holding data byte-identical to the `raw/trades/ES/` object of the "
          "same date; NQ had no data at all on those dates. Correct NQ data was "
          f"re-downloaded from Databento and, on {div.get('applied')}, "
          f"{div.get('method', 'written over the originals')}. The contaminated originals "
          f"remain in `{div.get('originals_recoverable_from', div.get('source'))}`, so "
          "nothing is unrecoverable.")
        a("")
        a(f"`{corr['config']}` is the record of that. It is evidence, not a mechanism: "
          "nothing reads it at runtime, no path is resolved anywhere, and each key below "
          "is simply the canonical object for its (symbol, date).")
        a("")
        a("| Date | Object | Was | Now | DBN | Size | Rows | sha256 |")
        a("|---|---|---|---|---|---|---|---|")
        for e in corr["entries"]:
            orig = e.get("original") or {}
            rep = e.get("replacement") or {}
            rows_ = f"{e['current_records_exact']:,}" if e["current_records_exact"] else "—"
            check = {True: " ✓", False: " **MISMATCH**", None: ""}[
                e["sha256_verified_against_bucket"]
            ]
            a(f"| {e['date']} | `{e['key'].split('/')[-1]}` | {orig.get('dbn_header_symbol')} "
              f"(v{orig.get('dbn_version')}, {human_bytes(orig.get('size_bytes', 0))}) "
              f"| {e['current_header_symbol']} | v{e['current_dbn_version']} "
              f"| {human_bytes(e['current_size_bytes'] or 0)} | {rows_} "
              f"| `{(rep.get('sha256') or '')[:12]}…`{check} |")
        a("")
        a(f"Digests are the ones recorded in `{corr['config']}` when the replacements were "
          "made; `scripts/inventory.py --verify-sha256` re-reads those six objects and "
          "checks them, and the live test tier does the same. The originals' digests were "
          "never computed — the bytes were overwritten before one was taken — so their "
          "size, DBN version and header symbol (carried above from the manifest generated "
          "while they were still in place) are the record instead.")
        a("")
        for job, prov in sorted(corr["job_provenance"].items()):
            a(f"**Databento job `{job}`** — {prov.get('dataset')} / {prov.get('schema')} / "
              f"{', '.join(prov.get('symbols', []))}, `stype_in={prov.get('stype_in')}`, "
              f"submitted {prov.get('submitted')}.")
            if prov.get("sidecars_purged_from"):
                a("")
                a(f"> The vendor sidecars for this job lived at "
                  f"`{prov['sidecars_purged_from']}` and were purged with the rest of that "
                  f"prefix; the query parameters are transcribed into `{corr['config']}`.")
            if prov.get("note"):
                a("")
                a(f"> {prov['note']}")
            a("")

    # 10 integrity
    a("## 10. Per-object integrity")
    a("")
    a("The in-place overwrite gave up `rclone check` against the source lake as a "
      "mirror-integrity test, so the repo has to answer *did anything in `raw/` change?* "
      "on its own.")
    a("")
    a(f"- {integ['objects_fingerprinted']:,} objects carry size, ETag and last-modified in "
      "`data/manifest.json`. R2 returns the plain MD5 as the ETag for a single-part "
      "upload, so this is a full fingerprint taken from `ListObjectsV2` at **zero "
      "egress** — no object is downloaded to compute it.")
    a(f"- multipart (composite) ETags found: **{len(integ['composite_etags'])}**"
      + (" — those are a hash of part hashes, not the object's MD5, and fall back to a "
         "size-only comparison" if integ["composite_etags"] else
         ", so every ETag here is a directly comparable MD5"))
    a(f"- every run compares the live listing against the committed manifest. Status this "
      f"run: **{integ['status']}**"
      + ("" if integ["comparable_baseline"] else " (no prior manifest to compare against)"))
    a("")
    if integ["changed"] or integ["added"] or integ["removed"]:
        a("| Key | Change | Classification |")
        a("|---|---|---|")
        for c in integ["changed"][:40]:
            what = "; ".join(
                f"{f} {v[0]} → {v[1]}" for f, v in sorted(c["changes"].items())
            )
            a(f"| `{c['key']}` | {what} | {c['classification']} |")
        for k in integ["added"][:20]:
            a(f"| `{k}` | added | — |")
        for k in integ["removed"][:20]:
            a(f"| `{k}` | removed | — |")
        a("")
        a("A change on an object recorded in the corrections file is the divergence "
          "described in §9. Anything else is a critical anomaly: either the bucket was "
          "modified without a record, or the record is missing an entry.")
        a("")
    else:
        a("No object changed since the committed manifest.")
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
    ap.add_argument("--verify-sha256", action="store_true",
                    help="re-read the corrected objects and check their digests against "
                    "config/corrections.yaml (~18 MB); off by default")
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
