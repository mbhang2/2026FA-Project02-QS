"""Crypto perpetual trades (Binance USDT-margined) behind the `Dataset` interface.

The second real adapter. It follows `nq.py`'s rules -- sessions from a committed
manifest rather than re-derived, a digest that pins bytes, a pre-registered split
read from disk and never invented, the holdout locked -- and differs only where
the market does:

* **A session is a UTC day**, 00:00 to 24:00 UTC, and one archive file is exactly
  one session. Crypto trades continuously, so there are no early closes and no
  halts; every day the archive published is a session.
* **Sizes are integer lots.** The engine's sizes are `int` (they were NQ
  contracts). One lot is `spec.lot` of the coin -- 0.001, Binance's quantity step
  for BTCUSDT and ETHUSDT -- so every archived trade is a whole number of lots and
  that is asserted, not rounded. P&L in the engine is then (price move) x lots, in
  USD per lot of `lot` coin; `lot` converts it to USD.
* **The aggressor is always known.** `side` is never `None`, so `bar.delta` is
  exact rather than an estimate with unknown-side volume excluded.

The digest is built from the SHA-256 the fetcher recorded per file
(`data/crypto-manifest.json`), so a ledger row pins the bytes it ran against.
Files are read from the local lake when present, else from R2 at the same key.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np

from ..types import NS_PER_SECOND, DataCapabilities, Side, Split, Tick, Ts
from .columns import SessionColumns
from .crypto_raw import read_funding, read_klines, read_trades
from .dataset import Calendar, HoldoutLock, calendar_path, load_calendar, sessions_for


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "config" / "calendars").is_dir():
            return p
    raise RuntimeError("cannot locate the repository root from " + str(here))


REPO_ROOT = _repo_root()
MANIFEST_PATH = REPO_ROOT / "data" / "crypto-manifest.json"
CACHE_ROOT = REPO_ROOT / "data" / "cache" / "crypto-sessions"
DEFAULT_LAKE = Path(os.environ.get("CRYPTO_LAKE", "~/dl/crypto")).expanduser()

# Bump when anything here changes what a cached session contains.
CACHE_VERSION = 1

_DAY_NS = 86_400 * NS_PER_SECOND
_TRADES_RE = re.compile(
    r"^crypto/raw/(?P<ex>[a-z]+)/trades/(?P<sym>[A-Z0-9]+)/(?P=sym)-aggTrades-(?P<ymd>\d{4}-\d{2}-\d{2})\.zip$")


def session_bounds_utc(session: dt.date) -> tuple[Ts, Ts]:
    """[00:00 UTC, 24:00 UTC) as an inclusive (open, close) pair of nanoseconds."""
    lo = int(dt.datetime(session.year, session.month, session.day,
                         tzinfo=dt.timezone.utc).timestamp()) * NS_PER_SECOND
    return lo, lo + _DAY_NS - 1


@dataclass
class CryptoSpec:
    symbol: str = "BTCUSDT"
    exchange: str = "binance"
    # Coin per lot. Every archived quantity must be a whole number of these.
    lot: float = 0.001
    cache: bool = True
    max_sessions: int | None = None
    # Plumbing only, as in NQSpec: a fixed prefix of sessions with no split
    # meaning, refused for the holdout and never scored.
    smoke_sessions: int | None = None


class CryptoDataset:
    """`Dataset` over one symbol's daily trade archives on one exchange."""

    def __init__(self, s3=None, bucket: str | None = None, spec: CryptoSpec | None = None,
                 manifest_path: Path = MANIFEST_PATH, lake: Path | None = None):
        self.spec = spec or CryptoSpec()
        if self.spec.exchange != "binance":
            raise NotImplementedError(
                "only Binance is served: it is the one venue with trades, funding and "
                "taker-split klines all archived (docs/datasets/crypto.md)")
        self.name = f"crypto-trades:{self.spec.symbol}"
        self._s3 = s3
        self._bucket = bucket or os.environ.get("R2_BUCKET", "")
        self._lake = lake or DEFAULT_LAKE
        self._manifest = json.loads(Path(manifest_path).read_text())
        self._lock = HoldoutLock()
        self._calendar: Calendar | None = None

        self._files: dict[dt.date, str] = {}
        self._sha: dict[str, str] = {}
        for key, row in self._manifest["files"].items():
            m = _TRADES_RE.match(key)
            if m and m.group("ex") == self.spec.exchange and m.group("sym") == self.spec.symbol:
                d = dt.date.fromisoformat(m.group("ymd"))
                self._files[d] = key
                self._sha[key] = row[1]
        if not self._files:
            raise ValueError(f"no {self.spec.exchange} {self.spec.symbol} trades in {manifest_path}")
        self._sessions = sorted(self._files)
        if self.spec.max_sessions:
            # Trimmed BEFORE the gap check, so the check is about the sessions
            # actually served: a prefix of a still-downloading history is whole.
            self._sessions = self._sessions[: self.spec.max_sessions]
        gaps = (self._sessions[-1] - self._sessions[0]).days + 1 - len(self._sessions)
        if gaps:
            # A missing day is not a holiday here: crypto has none. Serving across
            # one would put two non-adjacent days next to each other and every
            # multi-day feature would silently straddle the hole.
            missing = sorted(set(self._sessions[0] + dt.timedelta(days=i)
                                 for i in range((self._sessions[-1] - self._sessions[0]).days + 1))
                             - set(self._sessions))
            raise ValueError(f"{self.name}: {gaps} day(s) missing inside the range, first {missing[0]}; "
                             "fetch them before serving (scripts/crypto_fetch.py)")

    # ---- Dataset protocol -------------------------------------------------- #
    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(has_trades=True, has_quotes=False, has_book=False)

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
        if self.spec.smoke_sessions is not None:
            # The smoke prefix is served with no calendar, so it is scored without one.
            return [d.toordinal() for d in self._sessions[: self.spec.smoke_sessions]]
        _, live = self._split_positions(split, warmup)
        return [self._sessions[i].toordinal() for i in live]

    def warmup_sessions(self, split, warmup: int) -> set:
        if split is None or self.spec.smoke_sessions is not None:
            return set()
        warm, _ = self._split_positions(split, warmup)
        return {self._sessions[i].toordinal() for i in warm}

    def early_close_dates(self) -> frozenset[int]:
        return frozenset()          # a UTC day never closes early

    def unlock_holdout(self, reason: str) -> None:
        self._lock.unlock(reason)

    @property
    def holdout_lock(self) -> HoldoutLock:
        return self._lock

    def digest(self) -> str:
        payload = json.dumps({
            "adapter": "crypto-trades/1",
            "exchange": self.spec.exchange,
            "symbol": self.spec.symbol,
            "lot": self.spec.lot,
            "sessions": [d.isoformat() for d in self._sessions],
            "sha256": {self._files[d]: self._sha[self._files[d]] for d in self._sessions},
        }, sort_keys=True)
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
    def _fetch(self, key: str, *, keep: bool = True) -> Path:
        """A verified local copy of `key`: the lake if it holds it, else R2.

        `keep=False` is for trade files, whose decoded session cache replaces them:
        the caller deletes the copy once it is decoded, so the raw zip is not held
        twice on disk."""
        local = self._lake / key
        if local.exists():
            return local
        if self._s3 is None:
            raise RuntimeError(f"{key} is not in {self._lake} and no S3 client was given")
        dest = CACHE_ROOT / ("raw" if keep else "transient") / key
        if dest.exists():
            return dest
        body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        sha = hashlib.sha256(body).hexdigest()
        if sha != self._sha.get(key, sha):
            raise ValueError(f"{key} from R2 hashes to {sha}, the manifest says {self._sha[key]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.replace(tmp, dest)
        return dest

    def _aux_files(self, kind: str) -> list[str]:
        prefix = f"crypto/raw/{self.spec.exchange}/{kind}/{self.spec.symbol}/"
        return sorted(k for k in self._manifest["files"] if k.startswith(prefix))

    def funding_events(self) -> list[tuple[int, float]]:
        """(ts_ns, rate) for every funding settlement the manifest holds, in order.
        A long position pays `rate x notional` at each; a short receives it."""
        out: list[tuple[int, float]] = []
        for key in self._aux_files("funding"):
            df = read_funding(self.spec.exchange, self._fetch(key))
            out.extend(zip(df["ts_ns"].tolist(), df["rate"].tolist()))
        return sorted(set(out))

    def minute_bars(self, session) -> "np.ndarray":
        """The exchange's own 1-minute klines for a session, as a structured array.
        Used for reference prices (session open, mark at funding), not served to
        strategies, which build their own bars from ticks."""
        d = self._as_date(session)
        key = (f"crypto/raw/{self.spec.exchange}/klines_1m/{self.spec.symbol}/"
               f"{self.spec.symbol}-1m-{d.isoformat()}.zip")
        df = read_klines(self.spec.exchange, self._fetch(key))
        return df.to_records(index=False)

    def session_open_prices(self, sessions: Sequence[int]) -> dict[int, float]:
        """{session id: the open of its first 1-minute kline}. The per-session notional
        a crypto score divides by, so a return means the same at $16k and at $100k.
        Taken from the exchange's klines, not from the ticks a strategy is served, so
        it is a fact about the session rather than anything a run produced."""
        out: dict[int, float] = {}
        for s in sessions:
            bars = self.minute_bars(s)
            if len(bars) == 0:
                raise ValueError(f"{self.name}: no klines for session {self._as_date(s)}")
            out[int(s)] = float(bars["open"][0])
        return out

    def funding_series(self, sessions: Sequence[int]) -> list:
        """`FundingEvent`s inside `sessions`, each with the mark price at its instant:
        the open of the 1-minute kline containing it. Binance settles at 00:00, 08:00
        and 16:00 UTC, so every event falls inside a UTC-day session."""
        from ..scoring.metrics import FundingEvent

        wanted = {int(s) for s in sessions}
        by_day: dict[int, dict[int, float]] = {}
        out = []
        for ts, rate in self.funding_events():
            day = dt.datetime.fromtimestamp(ts / NS_PER_SECOND, tz=dt.timezone.utc).date().toordinal()
            if day not in wanted:
                continue
            if day not in by_day:
                bars = self.minute_bars(day)
                by_day[day] = dict(zip(bars["ts_ns"].tolist(), bars["open"].tolist()))
            minute = ts - ts % (60 * NS_PER_SECOND)
            mark = by_day[day].get(minute)
            if mark is None:
                raise ValueError(f"{self.name}: no kline at {minute} for the funding event at {ts}")
            out.append(FundingEvent(ts=int(ts), rate=float(rate), mark=float(mark)))
        return out

    # ---- loading ----------------------------------------------------------- #
    def load(self, session) -> list[Tick]:
        d = self._as_date(session)
        return self._ticks(d, *self._load_arrays(d))

    def _load_arrays(self, d: dt.date) -> tuple:
        """`(ts, price, size, side)` for one session, from the cache or the archive."""
        if d not in self._files:
            raise KeyError(f"no {self.name} file for session {d}")
        cached = self._read_arrays(d)
        if cached is not None:
            return cached
        src = self._fetch(self._files[d], keep=False)
        try:
            df = read_trades(self.spec.exchange, src)
        finally:
            if CACHE_ROOT in src.parents:
                src.unlink(missing_ok=True)
        lo, hi = session_bounds_utc(d)
        ns = df["ts_ns"].to_numpy(dtype=np.int64)
        keep = (ns >= lo) & (ns <= hi)
        if not keep.all():
            raise ValueError(f"{self._files[d]} holds {int((~keep).sum())} trade(s) outside {d} UTC")
        lots_f = df["size"].to_numpy(dtype=float) / self.spec.lot
        lots = np.rint(lots_f).astype(np.int64)
        if np.abs(lots_f - lots).max(initial=0.0) > 1e-6 or (lots <= 0).any():
            raise ValueError(f"{self._files[d]}: a quantity is not a positive whole number of "
                             f"{self.spec.lot} lots")
        arrays = (ns, df["price"].to_numpy(dtype=np.float64), lots,
                  df["side"].to_numpy(dtype=np.int8))
        self._write_cache(d, *arrays)
        return arrays

    @staticmethod
    def _ticks(d: dt.date, ns, prices, lots, sides) -> list[Tick]:
        key = d.toordinal()
        return [Tick(int(t), float(p), int(z), key, side=Side.BUY if s > 0 else Side.SELL)
                for t, p, z, s in zip(ns.tolist(), prices.tolist(), lots.tolist(), sides.tolist())]

    def ticks(self, split: Split | None = None, *, warmup: int = 0) -> Iterator[Tick]:
        if split is not None:
            self._lock.check(split)
            if self.spec.smoke_sessions is not None:
                if split is Split.HOLDOUT:
                    raise NotImplementedError("the smoke path never serves the holdout")
                for d in self._sessions[: self.spec.smoke_sessions]:
                    yield from self.load(d)
                return
            warm, live = self._split_positions(split, warmup)
            for i in warm + live:
                yield from self.load(self._sessions[i])
            return
        for d in self._sessions:
            yield from self.load(d)

    def session_columns(self, split: Split, *, warmup: int = 0) -> Iterator[SessionColumns]:
        """The same sessions as `ticks(split)`, as arrays. The holdout lock applies."""
        self._lock.check(split)
        if self.spec.smoke_sessions is not None:
            if split is Split.HOLDOUT:
                raise NotImplementedError("the smoke path never serves the holdout")
            days = self._sessions[: self.spec.smoke_sessions]
        else:
            warm, live = self._split_positions(split, warmup)
            days = [self._sessions[i] for i in warm + live]
        for d in days:
            ns, prices, lots, sides = self._load_arrays(d)
            if ns.size:
                # `_ticks` reads any positive code as a buy and anything else as a sell.
                yield SessionColumns(d.toordinal(), ns, prices, lots,
                                     np.where(sides > 0, 1, -1).astype(np.int8))

    # ---- cache ------------------------------------------------------------- #
    def _cache_path(self, d: dt.date) -> Path:
        key = self._files[d]
        digest = hashlib.sha256(f"{CACHE_VERSION}|{self.spec.lot}|{key}:{self._sha[key]}".encode()).hexdigest()[:12]
        return CACHE_ROOT / self.spec.exchange / self.spec.symbol / f"{d.isoformat()}-{digest}.npz"

    def _read_arrays(self, d: dt.date) -> tuple | None:
        if not self.spec.cache:
            return None
        path = self._cache_path(d)
        if not path.exists():
            return None
        with np.load(path, allow_pickle=False) as z:
            return z["ts"], z["price"], z["size"], z["side"]

    def _read_cache(self, d: dt.date) -> list[Tick] | None:
        arrays = self._read_arrays(d)
        return None if arrays is None else self._ticks(d, *arrays)

    def _write_cache(self, d: dt.date, ns, prices, lots, sides) -> None:
        """Atomic and content-keyed, for the same reasons as `NQDataset._write_cache`."""
        if not self.spec.cache:
            return
        path = self._cache_path(d)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        os.close(fd)
        try:
            buf = io.BytesIO()
            np.savez_compressed(buf, ts=ns, price=prices, size=lots, side=sides)
            Path(tmp).write_bytes(buf.getvalue())
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
