"""OKX BTC or ETH perpetual: trades and the top-of-book, from one venue (Amendment 20).

A session is a UTC day. OKX's trade and kline files are UTC+8 days, so a session is cut
from two files by timestamp. Only days with an OKX quote file are sessions: the dates
OKX did not publish are left out of the calendar rather than served without a book.

**The book is cached as memory-mapped arrays**, so a trial pays for the pages it reads
instead of decoding about three million rows a day. Levels 2 to 5 are read only when an
order is larger than the best level.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np

from .._roots import repo_root
from ..types import NS_PER_SECOND, DataCapabilities, Side, Split, Tick, Ts
from .columns import SessionColumns
from .crypto_raw import read_funding, read_klines, read_quotes, read_trades
from .dataset import Calendar, HoldoutLock, calendar_path, load_calendar, sessions_for

REPO_ROOT = repo_root()
MANIFEST_PATH = REPO_ROOT / "data" / "crypto-manifest.json"
QUOTES_MANIFEST_PATH = REPO_ROOT / "data" / "crypto-quotes-manifest.json"
CACHE_ROOT = REPO_ROOT / "data" / "cache" / "crypto-sessions" / "okx-book"
DEFAULT_LAKE = Path(os.environ.get("CRYPTO_LAKE", "~/dl/crypto")).expanduser()
CACHE_VERSION = 1
_DAY_NS = 86_400 * NS_PER_SECOND
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})\.(?:zip|npz)$")
_INST = {"BTCUSDT": "BTC-USDT-SWAP", "ETHUSDT": "ETH-USDT-SWAP"}


def session_bounds_utc(session: dt.date) -> tuple[Ts, Ts]:
    lo = int(dt.datetime(session.year, session.month, session.day,
                         tzinfo=dt.timezone.utc).timestamp()) * NS_PER_SECOND
    return lo, lo + _DAY_NS - 1


def to_lots(quantity: np.ndarray, lot: float, what: str) -> np.ndarray:
    """Coin quantities as whole lots. A fractional lot is refused, never rounded away."""
    raw = np.asarray(quantity, dtype=float) / lot
    lots = np.rint(raw)
    if raw.size and np.abs(raw - lots).max() > 1e-6:
        raise ValueError(f"{what}: a quantity is not a whole number of {lot} lots")
    return lots.astype(np.int64)


@dataclass
class OKXBookSpec:
    symbol: str = "BTCUSDT"
    exchange: str = "okx"
    lot: float = 0.0001
    contract_value: float = 0.01
    cache: bool = True


class SessionBook:
    """One session's best bid and offer. `levels` reads the full five on demand."""

    __slots__ = ("ts", "bid", "ask", "bid_qty", "ask_qty", "_deep", "_deep_rows")

    def __init__(self, ts, bid, ask, bid_qty, ask_qty, deep=None):
        self.ts, self.bid, self.ask, self.bid_qty, self.ask_qty = ts, bid, ask, bid_qty, ask_qty
        self._deep = deep
        self._deep_rows = None

    def row_at(self, ts: Ts) -> int:
        """The last row at or before `ts`, or -1."""
        return int(np.searchsorted(self.ts, ts, side="right")) - 1

    def levels(self, row: int, side: Side) -> tuple[np.ndarray, np.ndarray]:
        """(prices, lots) a taker on `side` would walk, best first."""
        if self._deep is None:
            return (np.array([self.ask[row] if side is Side.BUY else self.bid[row]]),
                    np.array([self.ask_qty[row] if side is Side.BUY else self.bid_qty[row]]))
        if self._deep_rows is None:
            self._deep_rows = self._deep()
        book = self._deep_rows
        key = "ask" if side is Side.BUY else "bid"
        return book[f"{key}_px"][row], book[f"{key}_lots"][row]


class OKXBookDataset:
    """`Dataset` over OKX trades, with `book_for(session)` for the book-aware fill model."""

    def __init__(self, s3=None, bucket: str | None = None, spec: OKXBookSpec | None = None,
                 manifest_path: Path = MANIFEST_PATH,
                 quotes_manifest_path: Path = QUOTES_MANIFEST_PATH, lake: Path | None = None):
        self.spec = spec or OKXBookSpec()
        if self.spec.exchange != "okx":
            raise ValueError("OKXBookDataset serves OKX only: trades and book must share a venue")
        if self.spec.symbol not in _INST:
            raise ValueError(f"no OKX instrument for {self.spec.symbol}")
        self.name = f"okx-book:{self.spec.symbol}"
        self._s3, self._bucket = s3, bucket or os.environ.get("R2_BUCKET", "")
        self._lake = lake or DEFAULT_LAKE
        self._lock = HoldoutLock()
        self._calendar: Calendar | None = None
        self._book_cache: tuple[int, SessionBook] | None = None
        manifest = json.loads(Path(manifest_path).read_text())["files"]
        quotes = json.loads(Path(quotes_manifest_path).read_text())["files"]
        self._sha = {k: v[1] for k, v in {**manifest, **quotes}.items()}
        symbol = self.spec.symbol
        self._trades = self._by_day(manifest, f"crypto/raw/okx/trades/{symbol}/")
        self._klines = self._by_day(manifest, f"crypto/raw/okx/klines_1m/{symbol}/")
        self._funding_keys = sorted(k for k in manifest
                                    if k.startswith(f"crypto/raw/okx/funding/{symbol}/"))
        self._quotes = self._by_day(quotes, f"crypto/compact/okx/quotes/{symbol}/")
        one = dt.timedelta(days=1)
        # A UTC day needs its quote file and the two UTC+8 files that cover it.
        self._sessions = sorted(
            d for d in self._quotes
            if all(x in self._trades and x in self._klines for x in (d, d + one)))
        if not self._sessions:
            raise ValueError(f"no OKX {symbol} sessions have both trades and quotes")

    @staticmethod
    def _by_day(files: dict, prefix: str) -> dict[dt.date, str]:
        out = {}
        for key in files:
            if key.startswith(prefix) and (m := _DATE.search(key)):
                out[dt.date.fromisoformat(m.group(1))] = key
        return out

    # ---- Dataset protocol -------------------------------------------------- #
    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(has_trades=True, has_quotes=False, has_book=True)

    def periods(self) -> Sequence[dt.date]:
        return tuple(self._sessions)

    def sessions(self) -> Sequence[dt.date]:
        return tuple(self._sessions)

    def calendar(self) -> Calendar:
        if self._calendar is None:
            self._calendar = load_calendar(REPO_ROOT / calendar_path(self.name), self._sessions)
        return self._calendar

    def _split_positions(self, split: Split, warmup: int):
        warm, live = sessions_for(self.calendar(), split, warmup)
        return list(warm), list(live)

    def scored_sessions(self, split: Split, warmup: int = 0) -> list[int]:
        _, live = self._split_positions(split, warmup)
        return [self._sessions[i].toordinal() for i in live]

    def warmup_sessions(self, split, warmup: int) -> set:
        if split is None:
            return set()
        warm, _ = self._split_positions(split, warmup)
        return {self._sessions[i].toordinal() for i in warm}

    def early_close_dates(self) -> frozenset[int]:
        return frozenset()

    def unlock_holdout(self, reason: str) -> None:
        self._lock.unlock(reason)

    @property
    def holdout_lock(self) -> HoldoutLock:
        return self._lock

    def digest(self) -> str:
        keys = []
        one = dt.timedelta(days=1)
        for d in self._sessions:
            keys += [self._quotes[d], self._trades[d], self._trades[d + one]]
        payload = json.dumps({"adapter": "okx-book/1", "symbol": self.spec.symbol,
                              "lot": self.spec.lot, "contract_value": self.spec.contract_value,
                              "sessions": [d.isoformat() for d in self._sessions],
                              "sha256": {k: self._sha[k] for k in sorted(set(keys))}},
                             sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def session_bounds(self, session) -> tuple[Ts, Ts]:
        return session_bounds_utc(self._as_date(session))

    @staticmethod
    def _as_date(session) -> dt.date:
        if isinstance(session, dt.date):
            return session
        if isinstance(session, str):
            return dt.date.fromisoformat(session)
        return dt.date.fromordinal(int(session))

    # ---- bytes ------------------------------------------------------------- #
    def _fetch(self, key: str) -> Path:
        local = self._lake / key
        if local.exists():
            return local
        dest = CACHE_ROOT / "raw" / key
        if dest.exists():
            return dest
        if self._s3 is None:
            raise RuntimeError(f"{key} is not in {self._lake} and no S3 client was given")
        body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        sha = hashlib.sha256(body).hexdigest()
        if sha != self._sha[key]:
            raise ValueError(f"{key} from R2 hashes to {sha}, the manifest says {self._sha[key]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.replace(tmp, dest)
        return dest

    def _key_digest(self, *keys: str) -> str:
        text = f"{CACHE_VERSION}|{self.spec.lot}|" + "|".join(f"{k}:{self._sha[k]}" for k in keys)
        return hashlib.sha256(text.encode()).hexdigest()[:12]

    # ---- trades ------------------------------------------------------------ #
    def _trade_path(self, d: dt.date) -> Path:
        one = dt.timedelta(days=1)
        digest = self._key_digest(self._trades[d], self._trades[d + one])
        return CACHE_ROOT / "trades" / self.spec.symbol / f"{d.isoformat()}-{digest}.npz"

    def _load_arrays(self, d: dt.date) -> tuple:
        if d not in self._quotes or d not in self._sessions:
            raise KeyError(f"no OKX session {d}")
        path = self._trade_path(d)
        if self.spec.cache and path.exists():
            with np.load(path, allow_pickle=False) as z:
                return z["ts"], z["price"], z["size"], z["side"]
        lo, hi = session_bounds_utc(d)
        parts = [read_trades("okx", self._fetch(self._trades[x]),
                             contract_value=self.spec.contract_value)
                 for x in (d, d + dt.timedelta(days=1))]
        ns = np.concatenate([p["ts_ns"].to_numpy(np.int64) for p in parts])
        px = np.concatenate([p["price"].to_numpy(float) for p in parts])
        qty = np.concatenate([p["size"].to_numpy(float) for p in parts])
        side = np.concatenate([p["side"].to_numpy(np.int8) for p in parts])
        keep = (ns >= lo) & (ns <= hi)
        ns, px, qty, side = ns[keep], px[keep], qty[keep], side[keep]
        order = np.argsort(ns, kind="stable")
        arrays = (ns[order], px[order], to_lots(qty[order], self.spec.lot, f"{d} trades"),
                  side[order].astype(np.int8))
        if self.spec.cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".npz")
            os.close(fd)
            try:
                np.savez_compressed(tmp, ts=arrays[0], price=arrays[1], size=arrays[2],
                                    side=arrays[3])
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return arrays

    def load(self, session) -> list[Tick]:
        d = self._as_date(session)
        ns, px, lots, sides = self._load_arrays(d)
        key = d.toordinal()
        return [Tick(int(t), float(p), int(z), key, side=Side.BUY if s > 0 else Side.SELL)
                for t, p, z, s in zip(ns.tolist(), px.tolist(), lots.tolist(), sides.tolist())]

    def _days(self, split: Split, warmup: int) -> list[dt.date]:
        self._lock.check(split)
        warm, live = self._split_positions(split, warmup)
        return [self._sessions[i] for i in warm + live]

    def ticks(self, split: Split, *, warmup: int = 0) -> Iterator[Tick]:
        for d in self._days(split, warmup):
            yield from self.load(d)

    def session_columns(self, split: Split, *, warmup: int = 0) -> Iterator[SessionColumns]:
        for d in self._days(split, warmup):
            ns, px, lots, sides = self._load_arrays(d)
            if ns.size:
                yield SessionColumns(d.toordinal(), ns, px, lots,
                                     np.where(sides > 0, 1, -1).astype(np.int8))

    # ---- book -------------------------------------------------------------- #
    def _book_dir(self, d: dt.date) -> Path:
        digest = self._key_digest(self._quotes[d])
        return CACHE_ROOT / "book" / self.spec.symbol / f"{d.isoformat()}-{digest}"

    def _read_session_quotes(self, d: dt.date) -> dict:
        q = read_quotes(self._fetch(self._quotes[d]), contract_value=self.spec.contract_value)
        lo, hi = session_bounds_utc(d)
        keep = (q["ts_ns"] >= lo) & (q["ts_ns"] <= hi)
        return {k: v[keep] for k, v in q.items()}

    def _build_book(self, d: dt.date, dest: Path) -> None:
        q = self._read_session_quotes(d)
        bid, ask = q["bid_px"][:, 0], q["ask_px"][:, 0]
        bq = to_lots(q["bid_qty"][:, 0], self.spec.lot, f"{d} quotes")
        aq = to_lots(q["ask_qty"][:, 0], self.spec.lot, f"{d} quotes")
        changed = np.ones(bid.size, dtype=bool)
        changed[1:] = ((bid[1:] != bid[:-1]) | (ask[1:] != ask[:-1])
                       | (bq[1:] != bq[:-1]) | (aq[1:] != aq[:-1]))
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix=dest.name + "."))
        try:
            for name, arr in (("ts", q["ts_ns"][changed].astype(np.int64)),
                              ("bid", bid[changed].astype(np.float64)),
                              ("ask", ask[changed].astype(np.float64)),
                              ("bid_qty", bq[changed].astype(np.int64)),
                              ("ask_qty", aq[changed].astype(np.int64))):
                np.save(tmp / f"{name}.npy", arr)
            np.save(tmp / "rows.npy", np.flatnonzero(changed))
            try:
                os.replace(tmp, dest)
            except OSError:              # another worker built the identical directory
                shutil.rmtree(tmp, ignore_errors=True)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    def book_for(self, session) -> SessionBook:
        """The session's book. One is held at a time, so memory is one session's pages."""
        d = self._as_date(session)
        key = d.toordinal()
        if self._book_cache is not None and self._book_cache[0] == key:
            return self._book_cache[1]
        if d not in self._quotes or d not in self._sessions:
            raise KeyError(f"no OKX book for session {d}")
        where = self._book_dir(d)
        if not where.exists():
            self._build_book(d, where)
        arrays = {n: np.load(where / f"{n}.npy", mmap_mode="r")
                  for n in ("ts", "bid", "ask", "bid_qty", "ask_qty")}

        def deep() -> dict:
            q = self._read_session_quotes(d)
            rows = np.load(where / "rows.npy")
            out = {}
            for side in ("bid", "ask"):
                out[f"{side}_px"] = q[f"{side}_px"][rows]
                out[f"{side}_lots"] = to_lots(q[f"{side}_qty"][rows], self.spec.lot,
                                              f"{d} quotes")
            return out

        book = SessionBook(arrays["ts"], arrays["bid"], arrays["ask"], arrays["bid_qty"],
                           arrays["ask_qty"], deep=deep)
        self._book_cache = (key, book)
        return book

    def warm(self, session) -> None:
        d = self._as_date(session)
        self._load_arrays(d)
        if not self._book_dir(d).exists():
            self._build_book(d, self._book_dir(d))

    def is_warm(self, session) -> bool:
        d = self._as_date(session)
        return self._trade_path(d).exists() and self._book_dir(d).exists()

    # ---- scoring references ------------------------------------------------ #
    def _minute_opens(self, d: dt.date) -> tuple[np.ndarray, np.ndarray]:
        one = dt.timedelta(days=1)
        digest = self._key_digest(self._klines[d], self._klines[d + one])
        path = CACHE_ROOT / "klines" / self.spec.symbol / f"{d.isoformat()}-{digest}.npz"
        if self.spec.cache and path.exists():
            with np.load(path, allow_pickle=False) as z:
                return z["ts"], z["open"]
        lo, hi = session_bounds_utc(d)
        frames = [read_klines("okx", self._fetch(self._klines[x])) for x in (d, d + one)]
        ns = np.concatenate([f["ts_ns"].to_numpy(np.int64) for f in frames])
        px = np.concatenate([f["open"].to_numpy(float) for f in frames])
        keep = (ns >= lo) & (ns <= hi)
        ns, px = ns[keep], px[keep]
        order = np.argsort(ns, kind="stable")
        ns, px = ns[order], px[order]
        if self.spec.cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".npz")
            os.close(fd)
            try:
                np.savez_compressed(tmp, ts=ns, open=px)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return ns, px

    def session_open_prices(self, sessions: Sequence[int]) -> dict[int, float]:
        out = {}
        for s in sessions:
            ns, px = self._minute_opens(self._as_date(s))
            if not ns.size:
                raise ValueError(f"{self.name}: no klines for session {self._as_date(s)}")
            out[int(s)] = float(px[0])
        return out

    def funding_events(self) -> list[tuple[int, float]]:
        out: list[tuple[int, float]] = []
        for key in self._funding_keys:
            df = read_funding("okx", self._fetch(key))
            out.extend(zip(df["ts_ns"].tolist(), df["rate"].tolist()))
        return sorted(set(out))

    def funding_series(self, sessions: Sequence[int]) -> list:
        """Each settlement in `sessions`, marked at the last 1-minute open at or before it.

        OKX's klines have known gaps of a minute or two, so the mark is as-of rather than
        an exact-minute lookup.
        """
        from ..scoring.metrics import FundingEvent

        wanted = {int(s) for s in sessions}
        marks: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        out = []
        for ts, rate in self.funding_events():
            day = dt.datetime.fromtimestamp(ts / NS_PER_SECOND, tz=dt.timezone.utc).date()
            key = day.toordinal()
            if key not in wanted:
                continue
            if key not in marks:
                marks[key] = self._minute_opens(day)
            ns, px = marks[key]
            i = int(np.searchsorted(ns, ts, side="right")) - 1
            if i < 0:
                raise ValueError(f"{self.name}: no kline at or before the funding event at {ts}")
            out.append(FundingEvent(ts=int(ts), rate=float(rate), mark=float(px[i])))
        return out
