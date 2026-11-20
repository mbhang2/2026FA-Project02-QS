#!/usr/bin/env python3
"""Download Binance, Bybit and OKX perpetual-futures archives into the local crypto lake.

    python scripts/crypto_fetch.py plan                       # what is upstream, what is missing, will it fit
    python scripts/crypto_fetch.py fetch --kind funding --kind klines_1m
    python scripts/crypto_fetch.py fetch --exchange binance --kind trades --start 2026-01-01
    python scripts/crypto_fetch.py fetch --to-r2 --drop-local # stream it all to R2 through a small disk
    python scripts/crypto_fetch.py verify                     # re-hash every file the manifest records

**No API key.** Every source is a public archive: Binance's `data.binance.vision`
bucket, Bybit's `public.bybit.com` directory listing, and OKX's
`/api/v5/public/market-data-history` index of files on `static.okx.com`.

**Where it goes.** Files land outside the repo, under `lake_root` from
`config/datasets/crypto.yaml` (default `~/dl/crypto`, or `$CRYPTO_LAKE`), at
`crypto/raw/<exchange>/<kind>/<symbol>/<upstream filename>`, the same key it gets in R2. What the repo keeps
is `data/crypto-manifest.json`: size, SHA-256 and source of every file, the
verification each passed, and the days each exchange did not publish. A file is
recorded only after it verifies, and written into place only then, so a crash
leaves a `.part` file and never a half-written one under its real name.

**Verification, per source.** Binance publishes a SHA-256 per file and it must
match. Bybit's ETags are multipart, so its file must match its Content-Length and
decompress to the end. OKX publishes neither, so its zip must pass `testzip`.

**It will not fill the disk.** `plan` sums what is missing; `fetch` stops
scheduling downloads once free space would drop below `--reserve-gib`.

**R2.** `fetch --to-r2` uploads each verified file to the same key in an R2
bucket. Credentials come from the `r2w` remote in `secrets/rclone.conf` (the
read-write key; `--r2-remote` picks another), else from `R2_ENDPOINT`,
`R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`. The bucket is `CRYPTO_R2_BUCKET`,
else `R2_BUCKET`, else the one in `secrets/r2.env`. It checks
the stored size, and records the bucket. `--drop-local` then deletes the local
copy, so the whole history passes through a small disk. `upload` pushes files
already on disk. A file counts as held if it is on disk or recorded in R2.
Tick trades are the bulk: from about 20 MB (Binance, OKX) to 80 MB (Bybit) per
symbol per day in 2026.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from xml.etree import ElementTree

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "datasets" / "crypto.yaml"
MANIFEST = ROOT / "data" / "crypto-manifest.json"
# Quotes are fetched on a different machine from trades (they are ~10x the bytes),
# so they keep their own manifest and two fetches never edit the same file.
QUOTES_MANIFEST = ROOT / "data" / "crypto-quotes-manifest.json"

BINANCE_LIST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BINANCE_FILES = "https://data.binance.vision"
BYBIT_FILES = "https://public.bybit.com"
OKX_API = "https://www.okx.com"
USER_AGENT = "qs-crypto-fetch/1"

KINDS = ("trades", "klines_1m", "funding", "metrics", "quotes")

# exchange -> kind -> (granularity, how the exchange names it)
SOURCES: dict[str, dict[str, tuple[str, str]]] = {
    "binance": {
        "trades": ("daily", "aggTrades"),
        "klines_1m": ("daily", "klines"),
        "funding": ("monthly", "fundingRate"),
        "metrics": ("daily", "metrics"),
    },
    "bybit": {"trades": ("daily", "trading")},
    "okx": {
        "trades": ("daily", "1"),       # module ids of market-data-history
        "klines_1m": ("daily", "2"),
        "funding": ("monthly", "3"),
        # Order-book quotes, the best 50 levels on every change ("tick-by-tick").
        # Compacted on download to the top 5 levels; see `compact_okx_quotes`.
        "quotes": ("daily", "6"),
    },
}

# The calendar day a file is named for, per exchange. OKX names files by its
# local day; a reader must re-bucket by timestamp, never by file.
DAY_BASIS = {"binance": "UTC", "bybit": "UTC", "okx": "UTC+8"}

MANIFEST_FIELDS = ["size_bytes", "sha256", "source_url", "verified_by", "fetched_at", "r2_bucket"]

_DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}(?:-\d{2})?)\.(?:zip|csv\.gz|npz)$")


@dataclass(frozen=True)
class RemoteFile:
    exchange: str
    kind: str
    symbol: str
    period: str              # YYYY-MM-DD (daily) or YYYY-MM (monthly), as the exchange names it
    url: str
    size: int | None         # exact where the listing gives it, else an estimate or None
    size_exact: bool
    filename: str

    @property
    def key(self) -> str:
        # Quotes are stored compacted, not as published, so they live under
        # `compact/` rather than `raw/`: the bytes are ours, derived by code.
        stage = "compact" if self.kind == "quotes" else "raw"
        return f"crypto/{stage}/{self.exchange}/{self.kind}/{self.symbol}/{self.filename}"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_session():
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    retry = Retry(total=5, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "HEAD"))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    s.headers["User-Agent"] = USER_AGENT
    return s


# --------------------------------------------------------------------------- #
# Listing: pure parsers (tested offline) and the loops that page through them
# --------------------------------------------------------------------------- #
def period_of(filename: str) -> str | None:
    m = _DATE_IN_NAME.search(filename)
    return m.group(1) if m else None


def in_range(period: str, start: dt.date, end: dt.date) -> bool:
    """A daily file is in range by its day; a monthly one if its month overlaps."""
    if len(period) == 10:
        return start <= dt.date.fromisoformat(period) <= end
    y, m = map(int, period.split("-"))
    first = dt.date(y, m, 1)
    last = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return first <= end and last >= start


def parse_s3_listing(xml_text: str) -> tuple[list[tuple[str, int]], bool]:
    """(key, size) pairs from one S3 ListBucket page, and whether it is truncated."""
    root = ElementTree.fromstring(xml_text)
    ns = {"s3": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    q = (lambda t: f"s3:{t}") if ns else (lambda t: t)
    items = [(c.findtext(q("Key"), namespaces=ns) or "", int(c.findtext(q("Size"), namespaces=ns) or 0))
             for c in root.findall(q("Contents"), ns)]
    truncated = (root.findtext(q("IsTruncated"), namespaces=ns) or "").lower() == "true"
    return items, truncated


def parse_bybit_listing(html: str, symbol: str) -> list[str]:
    return sorted(set(re.findall(rf'href="({re.escape(symbol)}\d{{4}}-\d{{2}}-\d{{2}}\.csv\.gz)"', html)))


def okx_quotes_entries(payload: dict) -> list[tuple[str, str, float]]:
    """(UTC date, url, sizeMB) for module 6, whose filename is the same every day
    (`BTC-USDT-SWAP.OK.csv.gz`), so the day comes from `dateTs` (UTC midnight)."""
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX market-data-history: {payload.get('code')} {payload.get('msg')}")
    out = []
    for block in payload.get("data", []):
        for detail in block.get("details", []):
            for g in detail.get("groupDetails", []):
                day = dt.datetime.fromtimestamp(int(g["dateTs"]) / 1000, tz=dt.timezone.utc).date()
                out.append((day.isoformat(), g["url"], float(g.get("sizeMB") or 0)))
    return out


def parse_okx_listing(payload: dict) -> list[tuple[str, str, float]]:
    """(filename, url, sizeMB) from one market-data-history response."""
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX market-data-history: {payload.get('code')} {payload.get('msg')}")
    out = []
    for block in payload.get("data", []):
        for detail in block.get("details", []):
            for g in detail.get("groupDetails", []):
                out.append((g["filename"], g["url"], float(g.get("sizeMB") or 0)))
    return out


def okx_inst_id(symbol: str) -> str:
    if not symbol.endswith("USDT"):
        raise ValueError(f"only USDT-margined perpetuals are mapped for OKX, got {symbol}")
    return f"{symbol[:-4]}-USDT-SWAP"


def list_binance(http, kind: str, symbol: str, start: dt.date, end: dt.date) -> list[RemoteFile]:
    gran, name = SOURCES["binance"][kind]
    prefix = f"data/futures/um/{gran}/{name}/{symbol}/" + ("1m/" if kind == "klines_1m" else "")
    out, marker = [], ""
    while True:
        r = http.get(BINANCE_LIST, params={"prefix": prefix, "marker": marker}, timeout=60)
        r.raise_for_status()
        items, truncated = parse_s3_listing(r.text)
        for key, size in items:
            fname = key.rsplit("/", 1)[-1]
            period = period_of(fname)
            if key.endswith(".zip") and period and in_range(period, start, end):
                out.append(RemoteFile("binance", kind, symbol, period, f"{BINANCE_FILES}/{key}",
                                      size, True, fname))
        if not truncated or not items:
            return out
        marker = items[-1][0]


def list_bybit(http, kind: str, symbol: str, start: dt.date, end: dt.date) -> list[RemoteFile]:
    base = f"{BYBIT_FILES}/trading/{symbol}/"
    r = http.get(base, timeout=60)
    r.raise_for_status()
    names = [n for n in parse_bybit_listing(r.text, symbol)
             if in_range(period_of(n) or "0000-01-01", start, end)]

    def head(n: str) -> RemoteFile:
        h = http.head(base + n, timeout=60)
        h.raise_for_status()
        size = int(h.headers.get("Content-Length", 0)) or None
        return RemoteFile("bybit", kind, symbol, period_of(n) or "", base + n, size, size is not None, n)

    with ThreadPoolExecutor(8) as pool:
        return list(pool.map(head, names))


def list_okx(http, kind: str, symbol: str, start: dt.date, end: dt.date) -> list[RemoteFile]:
    gran, module = SOURCES["okx"][kind]
    family = okx_inst_id(symbol).removesuffix("-SWAP")
    # The index refuses windows over 10 days (daily) or 10 months (monthly).
    step = dt.timedelta(days=9) if gran == "daily" else dt.timedelta(days=270)
    # Pad by a day: OKX names files by UTC+8 day and windows by its timestamp.
    lo, hi = start - dt.timedelta(days=1), end + dt.timedelta(days=1)
    seen: dict[str, RemoteFile] = {}
    while lo <= hi:
        w_end = min(lo + step, hi)
        ms = lambda d: int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
        params = {"module": module, "instType": "SWAP", "instFamilyList": family,
                  "dateAggrType": gran, "begin": ms(lo), "end": ms(w_end)}
        for attempt in range(5):
            r = http.get(f"{OKX_API}/api/v5/public/market-data-history", params=params, timeout=60)
            r.raise_for_status()
            payload = r.json()
            if payload.get("code") == "50011":          # rate limited
                time.sleep(2 ** attempt)
                continue
            break
        if kind == "quotes":
            for day, url, size_mb in okx_quotes_entries(payload):
                if in_range(day, start, end):
                    fname = f"{symbol}-quotes-top5-{day}.npz"
                    # `size` is the RAW download, which is what the disk guard needs.
                    seen[fname] = RemoteFile("okx", kind, symbol, day, url,
                                             int(size_mb * 1024 * 1024) or None, False, fname)
        else:
            for fname, url, size_mb in parse_okx_listing(payload):
                period = period_of(fname)
                if period and in_range(period, start, end):
                    seen[fname] = RemoteFile("okx", kind, symbol, period, url,
                                             int(size_mb * 1024 * 1024) or None, False, fname)
        lo = w_end + dt.timedelta(days=1)
        time.sleep(0.25)
    return sorted(seen.values(), key=lambda f: f.period)


LISTERS: dict[str, Callable] = {"binance": list_binance, "bybit": list_bybit, "okx": list_okx}


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
def load_manifest(path: Path = MANIFEST) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {
        "schema_version": "1",
        "generated_by": "scripts/crypto_fetch.py",
        "lake_layout": "crypto/raw/<exchange>/<kind>/<symbol>/<upstream filename> (same key locally and in R2)",
        "day_basis": DAY_BASIS,
        "files_fields": MANIFEST_FIELDS,
        "files": {},
        "instruments": {},
        "upstream_gaps": {},
        "coverage": {},
    }


def coverage(manifest: dict) -> dict:
    """First, last and count per exchange/kind/symbol, from the files recorded."""
    groups: dict[str, list[str]] = {}
    for key in manifest["files"]:
        _, _, ex, kind, sym, fname = key.split("/")
        groups.setdefault(f"{ex}/{kind}/{sym}", []).append(period_of(fname) or "")
    return {g: {"first": min(p), "last": max(p), "files": len(p)} for g, p in sorted(groups.items())}


def daily_gaps(periods: Iterable[str]) -> list[str]:
    """Days missing strictly between the first and last day an exchange published."""
    days = sorted(dt.date.fromisoformat(p) for p in periods if len(p) == 10)
    if not days:
        return []
    have = set(days)
    out, d = [], days[0]
    while d < days[-1]:
        if d not in have:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def save_manifest(manifest: dict, path: Path = MANIFEST) -> None:
    manifest["files"] = dict(sorted(manifest["files"].items()))
    manifest["coverage"] = coverage(manifest)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=False) + "\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Download and verify
# --------------------------------------------------------------------------- #
def verify_file(f: RemoteFile, path: Path, sha256: str, http) -> str:
    """Raise if the file at `path` is not what the exchange published; else say how it was checked."""
    size = path.stat().st_size
    if f.exchange == "binance":
        r = http.get(f.url + ".CHECKSUM", timeout=60)
        r.raise_for_status()
        expected = r.text.split()[0].lower()
        if expected != sha256:
            raise ValueError(f"sha256 {sha256} != published {expected}")
        if f.size_exact and f.size != size:
            raise ValueError(f"size {size} != listed {f.size}")
        return "sha256 matches the published CHECKSUM"
    if f.exchange == "bybit":
        if f.size_exact and f.size != size:
            raise ValueError(f"size {size} != Content-Length {f.size}")
        with gzip.open(path, "rb") as fh:
            while fh.read(1 << 22):
                pass
        return "size matches Content-Length; gzip decompresses to the end"
    if f.exchange == "okx":
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise ValueError(f"zip member {bad} fails its CRC")
        return "zip CRC check passes (OKX publishes no checksum)"
    raise ValueError(f.exchange)


QUOTES_ATTEMPTS = 3


def okx_quotes_url(http, symbol: str, day: str) -> str:
    """A fresh link for one day. The index's links are signed and expire within a
    few hours, and a full history takes far longer than that to download."""
    family = okx_inst_id(symbol).removesuffix("-SWAP")
    d = dt.date.fromisoformat(day)
    ms = int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    params = {"module": "6", "instType": "SWAP", "instFamilyList": family,
              "dateAggrType": "daily", "begin": ms, "end": ms}
    for attempt in range(6):
        r = http.get(f"{OKX_API}/api/v5/public/market-data-history", params=params, timeout=60)
        r.raise_for_status()
        payload = r.json()
        if payload.get("code") == "50011":
            time.sleep(2 ** attempt)
            continue
        for got_day, url, _ in okx_quotes_entries(payload):
            if got_day == day:
                return url
        raise RuntimeError(f"OKX lists no quotes file for {symbol} {day}")
    raise RuntimeError(f"OKX index rate-limited for {symbol} {day}")


def download_quotes(f: RemoteFile, lake: Path, http) -> tuple[RemoteFile, list]:
    """Download one day of OKX quotes, compact it to the top 5 levels, delete the raw file.

    OKX publishes no checksum. The raw file is hashed as it arrives and read to
    the end by the compaction, which refuses a file whose exchange time runs
    backwards by more than a second or leaves its day; both facts are recorded.
    """
    sys.path.insert(0, str(ROOT))
    from src.core.data.crypto_raw import compact_okx_quotes

    import requests

    dest = lake / f.key
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_name(dest.name + ".raw.csv.gz")
    # **Transient failures are retried here, with a fresh link each time.** The
    # first full run on a desktop lost 37 of 1,402 days to dropped connections
    # and truncated downloads (a truncated gzip fails as EOFError or a zlib
    # OSError while compacting); all of them downloaded on a second try. A file
    # that is complete but wrong -- a clock running back, a row outside its
    # day, an unknown header -- is a ValueError and is not retried.
    for attempt in range(QUOTES_ATTEMPTS):
        url = okx_quotes_url(http, f.symbol, f.period)
        h = hashlib.sha256()
        try:
            with http.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                with open(raw, "wb") as out:
                    for chunk in r.iter_content(1 << 20):
                        h.update(chunk)
                        out.write(chunk)
            raw_size = raw.stat().st_size
            facts = compact_okx_quotes(raw, dest, day=dt.date.fromisoformat(f.period))
            break
        except (requests.RequestException, EOFError, OSError) as exc:
            if attempt + 1 == QUOTES_ATTEMPTS:
                raise
            print(f"  retrying {f.period} after {type(exc).__name__}: {str(exc)[:120]}",
                  file=sys.stderr, flush=True)
            time.sleep(10 * (attempt + 1))
        finally:
            raw.unlink(missing_ok=True)
    c = hashlib.sha256()
    with open(dest, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            c.update(chunk)
    how = (f"compacted from {raw_size} B raw (sha256 {h.hexdigest()}): {facts['rows']} rows read to the "
           f"end, {facts['kept']} kept where the top {facts['levels']} levels changed; "
           f"{facts['backsteps']} exchange-time step(s) back, max {facts['max_backstep_ms']} ms; "
           f"{facts['crossed']} crossed row(s)")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f, [dest.stat().st_size, c.hexdigest(), url.split("?", 1)[0], how, now, None]


def download(f: RemoteFile, lake: Path, http) -> tuple[RemoteFile, list]:
    if f.kind == "quotes":
        return download_quotes(f, lake, http)
    dest = lake / f.key
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    with http.get(f.url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(part, "wb") as out:
            for chunk in r.iter_content(1 << 20):
                h.update(chunk)
                out.write(chunk)
    digest = h.hexdigest()
    try:
        how = verify_file(f, part, digest, http)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)
    url = f.url.split("?", 1)[0]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f, [dest.stat().st_size, digest, url, how, now, None]


@dataclass
class R2Target:
    client: object
    bucket: str
    drop_local: bool


RCLONE_CONF = ROOT / "secrets" / "rclone.conf"
R2_ENV = ROOT / "secrets" / "r2.env"


def r2_credentials(remote: str) -> dict[str, str]:
    """Endpoint and keys from an rclone remote if it exists, else from the environment.
    Never printed: a credential that reaches a log has leaked."""
    import configparser

    if RCLONE_CONF.exists():
        conf = configparser.ConfigParser()
        conf.read(RCLONE_CONF)
        if remote in conf:
            s = conf[remote]
            return {"endpoint_url": s["endpoint"], "aws_access_key_id": s["access_key_id"],
                    "aws_secret_access_key": s["secret_access_key"]}
    missing = [v for v in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY") if not os.environ.get(v)]
    if missing:
        sys.exit(f"no [{remote}] remote in {RCLONE_CONF} and no {', '.join(missing)} in the environment")
    return {"endpoint_url": os.environ["R2_ENDPOINT"], "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
            "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"]}


def r2_bucket() -> str:
    bucket = os.environ.get("CRYPTO_R2_BUCKET") or os.environ.get("R2_BUCKET", "")
    if not bucket and R2_ENV.exists():
        for line in R2_ENV.read_text().splitlines():
            if line.startswith("R2_BUCKET="):
                bucket = line.split("=", 1)[1].strip()
    if not bucket:
        sys.exit("no bucket: set CRYPTO_R2_BUCKET or R2_BUCKET")
    return bucket


def r2_target(drop_local: bool, remote: str = "r2w") -> R2Target:
    import boto3

    client = boto3.client("s3", region_name="auto", **r2_credentials(remote))
    return R2Target(client, r2_bucket(), drop_local)


def upload(key: str, rec: list, lake: Path, r2: R2Target) -> list:
    """Put one verified file at the same key in R2; confirm the stored size; maybe drop it locally."""
    path = lake / key
    r2.client.upload_file(str(path), r2.bucket, key, ExtraArgs={"Metadata": {"sha256": rec[1]}})
    head = r2.client.head_object(Bucket=r2.bucket, Key=key)
    if int(head["ContentLength"]) != rec[0] or head.get("Metadata", {}).get("sha256") != rec[1]:
        raise ValueError(f"R2 copy of {key} does not match: {head['ContentLength']} bytes")
    rec = list(rec[:5]) + [r2.bucket]
    if r2.drop_local:
        path.unlink()
    return rec


def fetch_one(f: RemoteFile, lake: Path, http, r2: R2Target | None) -> tuple[RemoteFile, list]:
    f, rec = download(f, lake, http)
    if r2 is not None:
        rec = upload(f.key, rec, lake, r2)
    return f, rec


def held(key: str, have: dict, lake: Path) -> bool:
    rec = have.get(key)
    return rec is not None and ((lake / key).exists() or (len(rec) > 5 and bool(rec[5])))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def lake_root(cfg: dict) -> Path:
    root = Path(os.environ.get("CRYPTO_LAKE") or cfg["lake_root"]).expanduser().resolve()
    if ROOT in (root, *root.parents):
        sys.exit(f"refusing lake root {root}: it is inside the repository (see docs/setup/R2_ACCESS.md §7)")
    return root


def selection(cfg: dict, args) -> list[tuple[str, str, str]]:
    out = []
    for ex, spec in cfg["exchanges"].items():
        if args.exchange and ex not in args.exchange:
            continue
        # `on_request` kinds (OKX quotes: ~450 MB a day before compaction) are
        # fetched only when named with --kind, never by a bare `fetch`.
        kinds = list(spec["kinds"]) + (list(spec.get("on_request", [])) if args.kind else [])
        for kind in kinds:
            if args.kind and kind not in args.kind:
                continue
            if kind not in SOURCES[ex]:
                sys.exit(f"{ex} has no {kind} archive")
            for sym in (args.symbol or cfg["symbols"]):
                out.append((ex, kind, sym))
    return out


def check_okx_contracts(cfg: dict, http, symbols: Iterable[str], manifest: dict) -> None:
    """Refuse to fetch OKX trades if the configured ctVal disagrees with the exchange."""
    configured = cfg["exchanges"]["okx"].get("contract_value", {})
    for sym in symbols:
        r = http.get(f"{OKX_API}/api/v5/public/instruments",
                     params={"instType": "SWAP", "instId": okx_inst_id(sym)}, timeout=60)
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            sys.exit(f"OKX lists no instrument {okx_inst_id(sym)}")
        live = float(data[0]["ctVal"])
        if configured.get(sym) != live:
            sys.exit(f"OKX ctVal for {sym} is {live}, config says {configured.get(sym)}; "
                     "fix config/datasets/crypto.yaml before fetching")
        manifest["instruments"].setdefault("okx", {})[sym] = {
            "inst_id": okx_inst_id(sym), "ct_val": live, "ct_val_ccy": data[0].get("ctValCcy"),
            "checked_at": dt.date.today().isoformat()}


def plan(cfg: dict, args, http) -> tuple[list[RemoteFile], dict[str, list[str]]]:
    start = dt.date.fromisoformat(args.start or cfg["start"])
    end_s = args.end or cfg.get("end")
    end = dt.date.fromisoformat(end_s) if end_s else dt.date.today()
    files, gaps = [], {}
    for ex, kind, sym in selection(cfg, args):
        listed = LISTERS[ex](http, kind, sym, start, end)
        print(f"  listed {ex:8s} {kind:10s} {sym:8s} {len(listed):5d} file(s)", file=sys.stderr)
        files.extend(listed)
        g = daily_gaps(f.period for f in listed)
        if g:
            gaps[f"{ex}/{kind}/{sym}"] = g
    return files, gaps


def summarise(files: list[RemoteFile], have: dict, lake: Path) -> list[RemoteFile]:
    missing = [f for f in files if not held(f.key, have, lake)]
    missing_keys = {f.key for f in missing}
    rows: dict[tuple[str, str, str], list[int]] = {}
    for f in files:
        r = rows.setdefault((f.exchange, f.kind, f.symbol), [0, 0, 0, 0])
        r[0] += 1
        if f.key in missing_keys:
            r[1] += 1
            r[2] += f.size or 0
            r[3] += 0 if f.size_exact else 1
    print(f"{'exchange':8s} {'kind':10s} {'symbol':8s} {'listed':>6s} {'missing':>7s} {'to fetch':>10s}")
    for (ex, kind, sym), (n, m, b, est) in sorted(rows.items()):
        print(f"{ex:8s} {kind:10s} {sym:8s} {n:6d} {m:7d} {b / 2**30:8.2f} GiB{' ~' if est else ''}")
    total = sum(f.size or 0 for f in missing)
    free = shutil.disk_usage(lake if lake.exists() else Path.home()).free
    print(f"\nto fetch: {len(missing)} file(s), {total / 2**30:.2f} GiB   "
          f"free at {lake}: {free / 2**30:.1f} GiB   (~ = OKX sizes are its own estimates)")
    return missing


def manifest_for(cfg: dict, args) -> Path:
    """Quotes have their own manifest; a fetch is all quotes or none."""
    if args.cmd in ("upload", "verify"):
        return QUOTES_MANIFEST if args.quotes else MANIFEST
    kinds = {k for _, k, _ in selection(cfg, args)}
    if "quotes" in kinds and len(kinds) > 1:
        sys.exit("fetch quotes on their own (--kind quotes): they keep a separate manifest, "
                 "data/crypto-quotes-manifest.json")
    return QUOTES_MANIFEST if kinds == {"quotes"} else MANIFEST


def cmd_plan(args) -> int:
    cfg, http = load_config(), http_session()
    lake = lake_root(cfg)
    files, gaps = plan(cfg, args, http)
    summarise(files, load_manifest(manifest_for(cfg, args))["files"], lake)
    for g, days in gaps.items():
        print(f"upstream gap {g}: {len(days)} day(s), first {days[0]}")
    return 0


def cmd_fetch(args) -> int:
    cfg, http = load_config(), http_session()
    lake = lake_root(cfg)
    lake.mkdir(parents=True, exist_ok=True)
    mpath = manifest_for(cfg, args)
    manifest = load_manifest(mpath)
    okx_syms = {s for ex, _, s in selection(cfg, args) if ex == "okx"}
    if okx_syms:
        check_okx_contracts(cfg, http, sorted(okx_syms), manifest)
    files, gaps = plan(cfg, args, http)
    for g, days in gaps.items():
        manifest["upstream_gaps"][g] = days
    missing = summarise(files, manifest["files"], lake)
    r2 = r2_target(args.drop_local, args.r2_remote) if args.to_r2 else None
    if args.drop_local and r2 is None:
        sys.exit("--drop-local only makes sense with --to-r2")
    reserve = int(args.reserve_gib * 2**30)
    budget = int(args.max_gib * 2**30) if args.max_gib else None

    lock = threading.Lock()
    done = failed = 0
    spent = 0
    stopped = ""
    queue = list(missing)
    t0 = time.time()
    with ThreadPoolExecutor(args.workers) as pool:
        running = {}
        try:
            while queue or running:
                while queue and len(running) < args.workers and not stopped:
                    f = queue[0]
                    need = f.size or 0
                    if shutil.disk_usage(lake).free - need < reserve:
                        stopped = f"free space would drop below the {args.reserve_gib} GiB reserve"
                        break
                    if budget is not None and spent + need > budget:
                        stopped = f"--max-gib {args.max_gib} reached"
                        break
                    queue.pop(0)
                    spent += need
                    running[pool.submit(fetch_one, f, lake, http, r2)] = f
                if not running:
                    break
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for fut in finished:
                    f = running.pop(fut)
                    try:
                        _, rec = fut.result()
                        with lock:
                            manifest["files"][f.key] = rec
                        done += 1
                    except Exception as e:                     # noqa: BLE001 -- reported, never recorded
                        failed += 1
                        print(f"FAILED {f.key}: {e}", file=sys.stderr)
                    if (done + failed) % 25 == 0:
                        save_manifest(manifest, mpath)
                        rate = (done + failed) / max(time.time() - t0, 1e-9)
                        print(f"  {done + failed}/{len(missing)}  {rate:.1f} files/s", file=sys.stderr)
        finally:
            save_manifest(manifest, mpath)
    print(f"fetched {done}, failed {failed}, left {len(queue)}" + (f" -- stopped: {stopped}" if stopped else ""))
    return 1 if failed else 0


def cmd_upload(args) -> int:
    """Push files already on disk and in the manifest, but not yet in R2."""
    cfg = load_config()
    lake = lake_root(cfg)
    mpath = manifest_for(cfg, args)
    manifest = load_manifest(mpath)
    r2 = r2_target(args.drop_local, args.r2_remote)
    todo = [k for k, rec in manifest["files"].items() if (lake / k).exists() and not (len(rec) > 5 and rec[5])]
    print(f"uploading {len(todo)} file(s) to r2://{r2.bucket}/")
    failed = 0
    try:
        with ThreadPoolExecutor(args.workers) as pool:
            futs = {pool.submit(upload, k, manifest["files"][k], lake, r2): k for k in todo}
            for i, fut in enumerate(futs, 1):
                k = futs[fut]
                try:
                    manifest["files"][k] = fut.result()
                except Exception as e:                         # noqa: BLE001
                    failed += 1
                    print(f"FAILED {k}: {e}", file=sys.stderr)
                if i % 25 == 0:
                    save_manifest(manifest, mpath)
    finally:
        save_manifest(manifest, mpath)
    return 1 if failed else 0


def cmd_verify(args) -> int:
    cfg = load_config()
    lake = lake_root(cfg)
    mpath = manifest_for(cfg, args)
    manifest = load_manifest(mpath)
    bad = 0
    r2 = None
    for key, (size, sha, *rest) in manifest["files"].items():
        p = lake / key
        if not p.exists() and rest[3:] and rest[3]:
            if r2 is None:
                r2 = r2_target(False, args.r2_remote)
            head = r2.client.head_object(Bucket=rest[3], Key=key)
            if int(head["ContentLength"]) != size or head.get("Metadata", {}).get("sha256") != sha:
                print(f"differs  r2://{rest[3]}/{key}")
                bad += 1
            continue
        if not p.exists():
            print(f"missing  {key}")
            bad += 1
            continue
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                h.update(chunk)
        if p.stat().st_size != size or h.hexdigest() != sha:
            print(f"differs  {key}")
            bad += 1
    print(f"{len(manifest['files']) - bad} of {len(manifest['files'])} file(s) match the manifest")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "fetch"):
        p = sub.add_parser(name)
        p.add_argument("--exchange", action="append", choices=sorted(SOURCES))
        p.add_argument("--kind", action="append", choices=KINDS)
        p.add_argument("--symbol", action="append")
        p.add_argument("--start", help="YYYY-MM-DD; default from the config")
        p.add_argument("--end", help="YYYY-MM-DD; default from the config, else today")
        if name == "fetch":
            p.add_argument("--workers", type=int, default=4)
            p.add_argument("--reserve-gib", type=float, default=5.0,
                           help="stop before free space drops below this")
            p.add_argument("--max-gib", type=float, help="stop after scheduling this much")
            p.add_argument("--to-r2", action="store_true", help="upload each verified file to R2")
            p.add_argument("--drop-local", action="store_true",
                           help="with --to-r2: delete the local copy once R2 holds it")
            p.add_argument("--r2-remote", default="r2w", help="rclone remote holding the write key")
    up = sub.add_parser("upload")
    up.add_argument("--workers", type=int, default=4)
    up.add_argument("--drop-local", action="store_true")
    up.add_argument("--r2-remote", default="r2w")
    up.add_argument("--quotes", action="store_true", help="the quotes manifest")
    ver = sub.add_parser("verify")
    ver.add_argument("--r2-remote", default="r2w")
    ver.add_argument("--quotes", action="store_true", help="the quotes manifest")
    args = ap.parse_args(argv)
    return {"plan": cmd_plan, "fetch": cmd_fetch, "upload": cmd_upload,
            "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
