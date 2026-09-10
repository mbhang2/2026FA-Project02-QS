"""NQ futures trades, behind the `Dataset` interface. The first real adapter.

Everything the simulator does is unchanged; only the source of ticks is.

**The risk this file exists to manage.** Task 7 found that Task 6's causal
attribution was wrong, and the cause was a flaw in the synthetic generator --
the one input the calibration rig treated as fixed and never varied. That
generator was forty readable lines of numpy. This dataset is 45 GiB of vendor
binary nobody can read by eye, the same class of error is available, and it is
much harder to see. So the documented facts are *asserted* here rather than
assumed, and the manifest is *read* rather than re-derived:

* sessions come from `data/manifest.json`, computed the way `scripts/inventory.py`
  computes them, not recalculated from a calendar;
* the digest is built from the manifest's per-object ETags, so a ledger row pins
  the bytes it ran against;
* symbol identity is read from each object's DBN header, never from its path;
* the DBN reading rules -- double zstd, never seek by fixed offset -- live in
  `dbn.py` and are shared with the inventory script.

**Sessions straddle files.** A CME session runs 17:00 CT on D-1 to 16:00 CT on D
and the objects are split on UTC day, so every session is assembled from exactly
two of them. Sunday and holiday-eve objects hold only the evening open.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .dataset import Calendar, HoldoutLock, SplitBounds, calendar_path
from ..types import NS_PER_SECOND, DataCapabilities, Side, Split, Tick, Ts
from . import dbn

def _repo_root() -> Path:
    """Walk up to the marker rather than counting directories.

    `parents[3]` was correct until this file moved one level deeper, and it would
    have kept resolving to *a* directory rather than failing -- the manifest would
    simply not have been found. Counting parents is a hazard in a tree that is
    allowed to be reorganised.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parents[-1]


REPO_ROOT = _repo_root()
MANIFEST_PATH = REPO_ROOT / "data" / "manifest.json"
CORRECTIONS_PATH = REPO_ROOT / "config" / "corrections.yaml"
CACHE_ROOT = REPO_ROOT / "data" / "cache" / "sessions"

CME_TZ = ZoneInfo("America/Chicago")
UTC = dt.timezone.utc

# The CME session. 17:00 CT the previous calendar day to 16:00 CT on the session
# date, with the daily halt from 16:00 to 17:00 CT. Local times, deliberately:
# the UTC bounds move by an hour at each DST transition and hardcoding them is
# the documented way to get this wrong.
SESSION_OPEN = dt.time(17, 0)
SESSION_CLOSE = dt.time(16, 0)

KEY_RE = re.compile(r"^raw/trades/(?P<sym>[A-Z]+)/glbx-mdp3-(?P<ymd>\d{8})\.trades\.dbn\.zst$")

# Bump when anything here changes what a cached session contains. The cache key
# carries it, so old entries are ignored rather than silently reused.
CACHE_VERSION = 1


def _date(ymd: str) -> dt.date:
    return dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))


def session_bounds_utc(session: dt.date) -> tuple[Ts, Ts]:
    """(open, close) in UTC nanoseconds for a CME session date.

    Built from local wall-clock times and converted, so the bounds shift with
    CST/CDT rather than being pinned to a UTC offset that is right for half the
    year.
    """
    open_local = dt.datetime.combine(
        session - dt.timedelta(days=1), SESSION_OPEN, tzinfo=CME_TZ
    )
    close_local = dt.datetime.combine(session, SESSION_CLOSE, tzinfo=CME_TZ)
    to_ns = lambda x: int(x.astimezone(UTC).timestamp()) * NS_PER_SECOND  # noqa: E731
    return to_ns(open_local), to_ns(close_local)


@dataclass(frozen=True)
class ContractSelection:
    """Which contract a session's ticks come from, and why.

    `stype_in=PARENT` means every NQ contract is in the file -- outrights, spreads
    and back months. Choosing one is a **research convention, not a fact**, so it
    lives in immutable code with the rule written down, and the choice is
    recorded per session so a roll shows up as data rather than being inferred
    from a chart.
    """

    session: str
    instrument_id: int
    raw_symbol: str
    volume: int
    trades: int
    share_of_session_volume: float
    rule: str = "highest traded volume among outright contracts in the session"

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class NQSpec:
    symbol: str = "NQ"
    warmup_sessions: int = 0
    # Spreads are separate instruments in a PARENT file (`NQU5-NQZ5`). They are
    # not the outright and must not win a volume comparison with it.
    exclude_spreads: bool = True
    cache: bool = True
    max_sessions: int | None = None
    # Plumbing only. Set to serve a fixed prefix of sessions to the engine
    # without a split calendar, so the adapter can be smoke-tested end to end.
    # It is not a split: it has no train/validation/holdout meaning, it refuses
    # the holdout outright, and nothing scored may use it.
    smoke_sessions: int | None = None


class NQDataset:
    """`Dataset` implementation over `raw/trades/NQ/` in the R2 bucket."""

    def __init__(
        self,
        s3=None,
        bucket: str | None = None,
        spec: NQSpec | None = None,
        manifest_path: Path = MANIFEST_PATH,
    ):
        import os

        self.spec = spec or NQSpec()
        self.name = f"nq-trades:{self.spec.symbol}"
        self._s3 = s3
        self._bucket = bucket or os.environ.get("R2_BUCKET", "")
        self._manifest = json.loads(Path(manifest_path).read_text())
        self._lock = HoldoutLock()
        self._contracts: dict[str, ContractSelection] = {}

        files = self._manifest["files"]
        self._files: dict[dt.date, str] = {}
        self._etag: dict[str, str] = {}
        for key, row in files.items():
            m = KEY_RE.match(key)
            if not m or m.group("sym") != self.spec.symbol:
                continue
            header = row[3]
            # Symbol identity from the DBN header, never the path. The six
            # corrected objects made this concrete; the guard is that the
            # mismatch set is empty, and an adapter must not quietly reintroduce
            # what the guard exists to catch.
            if header is None or header.split(".")[0] != self.spec.symbol:
                raise ValueError(
                    f"{key} carries DBN header {header!r}, not {self.spec.symbol}. "
                    "The manifest should have no such object; regenerate it."
                )
            d = _date(m.group("ymd"))
            self._files[d] = key
            self._etag[key] = row[4]

        if not self._files:
            raise ValueError(
                f"no {self.spec.symbol} trades objects in {manifest_path}"
            )
        self._sessions = self._assemblable()
        if self.spec.max_sessions:
            self._sessions = self._sessions[: self.spec.max_sessions]

    # ---- what is assemblable ---------------------------------------------- #
    def _assemblable(self) -> list[dt.date]:
        """The manifest's own session list, checked against the objects present.

        Not re-derived from a trading calendar. Doing that would be a second
        implementation of "which dates are sessions", and the two would disagree
        on exactly the days that matter: a Sunday or holiday-eve object is a file
        date with a predecessor, and is not a session. That mistake is worth one
        line of code and several hours of confusion.
        """
        cov = self._manifest["instrument_coverage"][self.spec.symbol]["sessions"]
        dates = [dt.date.fromisoformat(x) for x in cov["dates"]]
        if len(dates) != cov["assemblable"]:
            raise ValueError(
                f"the manifest lists {len(dates)} session dates but records "
                f"{cov['assemblable']} assemblable; regenerate it"
            )
        for d in dates:
            for needed in (d - dt.timedelta(days=1), d):
                if needed not in self._files:
                    raise ValueError(
                        f"session {d} needs the {needed} object and the manifest "
                        "does not list one; the two disagree and it matters which"
                    )
        return dates

    # ---- Dataset protocol -------------------------------------------------- #
    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(has_trades=True, has_quotes=False, has_book=False)

    def periods(self) -> Sequence[dt.date]:
        return tuple(self._sessions)

    def sessions(self) -> Sequence[dt.date]:
        return tuple(self._sessions)

    def calendar(self) -> Calendar:
        raise NotImplementedError(
            f"{calendar_path(self.name)} is not written. The split is "
            "pre-registered by a human before any outcome is observed, and this "
            "adapter must not invent one."
        )

    def unlock_holdout(self, reason: str) -> None:
        self._lock.unlock(reason)

    @property
    def holdout_lock(self) -> HoldoutLock:
        return self._lock

    def digest(self) -> str:
        """Pins the actual bytes, from the manifest's per-object ETags.

        R2 returns the plain MD5 as the ETag for a single-part upload, so this is
        a fingerprint of the objects this dataset would serve -- not of the code,
        and not of when it ran.
        """
        payload = json.dumps(
            {
                "adapter": "nq-trades/1",
                "symbol": self.spec.symbol,
                "sessions": [d.isoformat() for d in self._sessions],
                "etags": {self._files[d]: self._etag[self._files[d]] for d in self._sessions},
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def session_bounds(self, session) -> tuple[Ts, Ts]:
        return session_bounds_utc(self._as_date(session))

    def _as_date(self, session) -> dt.date:
        """A session is a date. `Tick.session` carries it as a Gregorian ordinal
        because the engine's session key is an int, so an int here is an ordinal
        and never a position -- an index would silently mean something different
        depending on how many sessions the spec happened to trim to."""
        if isinstance(session, dt.date):
            return session
        if isinstance(session, str):
            return dt.date.fromisoformat(session)
        return dt.date.fromordinal(int(session))

    # ---- loading ----------------------------------------------------------- #
    def contract_for(self, session) -> ContractSelection | None:
        d = self._as_date(session)
        if d.isoformat() not in self._contracts:
            self.load(d)
        return self._contracts.get(d.isoformat())

    def load(self, session) -> list[Tick]:
        """One session, assembled from its two UTC-day objects.

        Refuses anything outside the requested window: the ticks are filtered to
        [17:00 CT on D-1, 16:00 CT on D] before anything else sees them.
        """
        d = self._as_date(session)
        if d not in self._files:
            raise KeyError(f"no {self.spec.symbol} object for session {d}")
        cached = self._read_cache(d)
        if cached is not None:
            return cached

        lo, hi = session_bounds_utc(d)
        frames = []
        for day in (d - dt.timedelta(days=1), d):
            key = self._files.get(day)
            if key is None:
                raise KeyError(f"session {d} needs {day} and it is not in the bucket")
            frames.append(self._read_day(key))
        df = pd.concat(frames)

        ns = df.index.view("int64") if hasattr(df.index, "view") else df.index.astype("int64")
        keep = (ns >= lo) & (ns <= hi)
        df = df[keep]
        if df.empty:
            raise ValueError(f"session {d} assembled to nothing between {lo} and {hi}")

        sel = self._select_contract(d, df)
        self._contracts[d.isoformat()] = sel
        df = df[df["instrument_id"] == sel.instrument_id]

        ticks = self._to_ticks(df, d)
        self._write_cache(d, ticks, sel)
        return ticks

    def _read_day(self, key: str) -> pd.DataFrame:
        if self._s3 is None:
            from .dataset import Dataset  # noqa: F401  (documentation only)

            raise RuntimeError(
                "this dataset needs an S3 client; construct it with NQDataset(s3, bucket)"
            )
        df = dbn.read_full_df(self._s3, self._bucket, key)
        # `ts_recv` is the index and `ts_event` a column, both datetime64[ns, UTC].
        # Sorting by the index is what "in ts_recv order" means; the vendor file is
        # already in that order and this makes the assumption explicit.
        return df.sort_index(kind="stable")

    def _select_contract(self, session: dt.date, df: pd.DataFrame) -> ContractSelection:
        """Front month by traded volume. A convention, stated.

        Highest volume among *outright* contracts: spreads are separate
        instruments in a PARENT file and must not win the comparison.
        """
        sub = df
        if self.spec.exclude_spreads:
            sub = df[~df["symbol"].astype(str).str.contains("-", regex=False)]
        if sub.empty:
            sub = df
        by = sub.groupby("instrument_id").agg(
            volume=("size", "sum"), trades=("size", "size"),
            raw_symbol=("symbol", "first"),
        )
        top = by.sort_values(["volume", "trades"], ascending=False).iloc[0]
        instrument_id = int(by.sort_values(["volume", "trades"], ascending=False).index[0])
        total = float(df["size"].sum()) or 1.0
        return ContractSelection(
            session=session.isoformat(),
            instrument_id=instrument_id,
            raw_symbol=str(top["raw_symbol"]),
            volume=int(top["volume"]),
            trades=int(top["trades"]),
            share_of_session_volume=round(float(top["volume"]) / total, 6),
        )

    def _to_ticks(self, df: pd.DataFrame, session: dt.date) -> list[Tick]:
        ns = df.index.view("int64") if hasattr(df.index, "view") else df.index.astype("int64")
        prices = df["price"].to_numpy(dtype=float)
        sizes = df["size"].to_numpy(dtype=np.int64)
        sides = df["side"].astype(str).to_numpy()
        key = session.toordinal()
        side_map = {"B": Side.BUY, "A": Side.SELL}
        return [
            Tick(
                ts=int(ns[i]),
                price=float(prices[i]),
                size=int(sizes[i]),
                session=key,
                side=side_map.get(sides[i]),
            )
            for i in range(len(df))
        ]

    def ticks(self, split: Split | None = None, *, warmup: int = 0) -> Iterator[Tick]:
        """Every tick of every session, in order.

        A `split` is refused unless `spec.smoke_sessions` is set. The split
        calendar is pre-registered by a human before any outcome is observed and
        is not written yet, so there is no honest way to serve one -- and a
        plausible-looking default would be worse than an error, because every
        number computed from it would look fine.
        """
        if split is not None:
            self._lock.check(split)
            if self.spec.smoke_sessions is None:
                raise NotImplementedError(
                    "no split calendar exists for this dataset yet; iterate "
                    f"sessions explicitly, or write {calendar_path(self.name)}"
                )
            if split is Split.HOLDOUT:
                raise NotImplementedError("the smoke path never serves the holdout")
            for d in self._sessions[: self.spec.smoke_sessions]:
                yield from self.load(d)
            return
        for d in self._sessions:
            yield from self.load(d)

    def warmup_sessions(self, split, warmup: int) -> set:
        return set()

    # ---- cache ------------------------------------------------------------- #
    def _cache_path(self, session: dt.date) -> Path:
        return (
            CACHE_ROOT
            / self.spec.symbol
            / f"{session.isoformat()}-{self._session_digest(session)}.npz"
        )

    def _session_digest(self, session: dt.date) -> str:
        """Keyed by the ETags of the two objects the session is built from, plus
        the code version.

        Invalidated by content, never by mtime: a stale cached session that
        silently disagrees with the bucket is the worst available failure,
        because every downstream number stays plausible.
        """
        keys = [self._files[session - dt.timedelta(days=1)], self._files[session]]
        payload = f"{CACHE_VERSION}|" + "|".join(f"{k}:{self._etag[k]}" for k in keys)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def _read_cache(self, session: dt.date) -> list[Tick] | None:
        if not self.spec.cache:
            return None
        path = self._cache_path(session)
        if not path.exists():
            return None
        with np.load(path, allow_pickle=False) as z:
            ns, prices, sizes, sides = z["ts"], z["price"], z["size"], z["side"]
            meta = json.loads(str(z["meta"]))
        self._contracts[session.isoformat()] = ContractSelection(**meta)
        key = session.toordinal()
        by_code = {1: Side.BUY, 2: Side.SELL}
        return [
            Tick(int(ns[i]), float(prices[i]), int(sizes[i]), key,
                 side=by_code.get(int(sides[i])))
            for i in range(len(ns))
        ]

    def _write_cache(self, session: dt.date, ticks: list[Tick], sel: ContractSelection) -> None:
        if not self.spec.cache:
            return
        path = self._cache_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        code = {Side.BUY: 1, Side.SELL: 2}
        np.savez_compressed(
            path,
            ts=np.array([t.ts for t in ticks], dtype=np.int64),
            price=np.array([t.price for t in ticks], dtype=np.float64),
            size=np.array([t.size for t in ticks], dtype=np.int32),
            side=np.array([code.get(t.side, 0) for t in ticks], dtype=np.int8),
            meta=json.dumps(sel.as_dict()),
        )
