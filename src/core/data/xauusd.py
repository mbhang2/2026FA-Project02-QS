"""XAUUSD spot gold ticks behind the `Dataset` interface. The second real adapter.

Source: local Parquet, one file per trading day at
`Gowtam_L1/Data/raw/Ticks/{YYYY}/{MM}/{DD}.parquet`, columns `timestamp`
(datetime64[ns, UTC]), `bid`, `bid_vol`, `ask`, `ask_vol`.

This is a **quotes** feed, not a trades feed, and the difference is stated rather
than papered over:

* There is no aggressor: `Tick.side` is always `None`. The `Tick` docstring warns
  that a strategy which assumes "unknown side" is "buy" is the order-flow defect
  it cannot fold over; there is nothing to condition on here.
* There is no traded size: `bid_vol`/`ask_vol` are order-book depth samples, not
  executions. `Tick.size` is the conventional unit 1 and a bar's `volume` is a
  tick count, matching the L1 harness that sat on this same corpus.
* Price is the mid `(bid + ask) / 2`.

Conventions (research choices, written down because they are not facts):

* A **session** is one trading day -- one file -- the L1 rollover convention
  (18:30 UTC). `session_bounds` are the file's actual first/last tick, so a
  holiday or an early close needs no separate calendar; the bounds are whatever
  the quote stream actually covered that day.
* The feed declares `has_trades=False, has_quotes=True`, so the engine must be
  pointed at a *quotes* fill model (`quotes-unfavorable`), not the trades one.
  That model fills a market order at the tick's mid: the bid/ask spread is NOT
  walked on the fill, so the half-spread cost is folded into the profile's
  `commission_per_contract` rather than lost. See `src/core/engine/fills.py`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np
import pandas as pd

from .dataset import (
    Calendar,
    HoldoutLock,
    calendar_path,
    load_calendar,
    sessions_for,
)
from ..types import DataCapabilities, Side, Split, Tick, Ts


def _repo_root() -> Path:
    """Walk up to the repo marker rather than counting directories."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parents[-1]


REPO_ROOT = _repo_root()
DEFAULT_DATA_DIR = REPO_ROOT / "Gowtam_L1" / "Data" / "raw" / "Ticks"

# Bump when anything changes what a tick stream or a digest contains, so a stale
# cache (if one is added later) is ignored rather than silently reused.
ADAPTER_VERSION = 1


@dataclass(frozen=True)
class XAUUSDSpec:
    # Where the per-day parquet files live. Relative to the repo root when it is
    # relative, absolute otherwise.
    data_dir: str = "Gowtam_L1/Data/raw/Ticks"
    warmup_sessions: int = 0
    max_sessions: int | None = None
    cache: bool = True

    # Plumbing only: serve a fixed prefix of sessions with no split meaning, so
    # the adapter can be smoke-tested without a calendar. Refuses the holdout.
    smoke_sessions: int | None = None


class XAUUSDDataset:
    """`Dataset` implementation over the local XAUUSD quote corpus."""

    def __init__(self, spec: XAUUSDSpec | None = None):
        self.spec = spec or XAUUSDSpec()
        self.name = "xauusd"
        root = Path(self.spec.data_dir)
        if not root.is_absolute():
            root = REPO_ROOT / self.spec.data_dir
        self._root = root
        if not self._root.exists():
            raise FileNotFoundError(
                f"XAUUSD data directory {self._root} not found. Point "
                "`data_dir` at the raw ticks corpus ({YYYY}/{MM}/{DD}.parquet)."
            )
        self._lock = HoldoutLock()
        self._calendar: Calendar | None = None
        self._bounds_cache: dict[int, tuple[Ts, Ts]] = {}
        self._tick_cache: dict[int, list[Tick]] = {}

        self._sessions = self._discover()
        if not self._sessions:
            raise ValueError(f"no per-day parquet files under {self._root}")
        if self.spec.max_sessions:
            self._sessions = self._sessions[: self.spec.max_sessions]

    # ---- discovery --------------------------------------------------------- #
    def _discover(self) -> list[dt.date]:
        """Every `<day>.parquet` under the corpus, as a sorted list of dates.

        The filename is the session's calendar date; the file itself holds the
        quote stream that rolled through it. Sorted so a session *position* in
        the calendar is chronological.
        """
        dates: list[dt.date] = []
        for path in sorted(self._root.glob("*/*/*.parquet")):
            ymd = "".join(path.parts[-3:]).removesuffix(".parquet")
            if len(ymd) == 8:
                try:
                    dates.append(dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:])))
                except ValueError as exc:
                    raise ValueError(
                        f"unparsable date in path {path}; expected YYYYMMDD.parquet"
                    ) from exc
        return sorted(dates)

    def _path(self, session: dt.date) -> Path:
        return self._root / f"{session.year:04d}" / f"{session.month:02d}" / \
            f"{session.day:02d}.parquet"

    # ---- Dataset protocol -------------------------------------------------- #
    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(has_trades=False, has_quotes=True, has_book=False)

    def periods(self) -> Sequence[dt.date]:
        return tuple(self._sessions)

    def sessions(self) -> Sequence[dt.date]:
        return tuple(self._sessions)

    def calendar(self) -> Calendar:
        if self._calendar is None:
            self._calendar = load_calendar(
                REPO_ROOT / calendar_path(self.name), self._sessions
            )
        return self._calendar

    def digest(self) -> str:
        """Pins the bytes the dataset would serve: session list plus, per file,
        its size and the first 4 KiB. Not the code, not the clock."""
        h = hashlib.sha256()
        for d in self._sessions:
            p = self._path(d)
            st = p.stat()
            with p.open("rb") as fh:
                head = fh.read(4096)
            h.update(f"{d.isoformat()}|{st.st_size}|".encode())
            h.update(hashlib.sha256(head).digest())
        h.update(ADAPTER_VERSION.to_bytes(4, "big"))
        return h.hexdigest()[:16]

    # ---- session identity -------------------------------------------------- #
    def _split_positions(self, split: Split, warmup: int):
        warm, live = sessions_for(self.calendar(), split, warmup)
        return list(warm), list(live)

    def _ordinal(self, position: int) -> int:
        return self._sessions[position].toordinal()

    def _as_date(self, session) -> dt.date:
        if isinstance(session, dt.date):
            return session
        if isinstance(session, str):
            return dt.date.fromisoformat(session)
        return dt.date.fromordinal(int(session))

    def scored_sessions(self, split: Split, warmup: int = 0) -> list[int]:
        _, live = self._split_positions(split, warmup)
        return [self._ordinal(i) for i in live]

    def warmup_sessions(self, split: Split, warmup: int) -> set[int]:
        if split is None:
            return set()
        warm, _ = self._split_positions(split, warmup)
        return {self._ordinal(i) for i in warm}

    def early_close_dates(self) -> frozenset[int]:
        # A 24h spot quote stream has no exchange-declared early close; the
        # session bounds are the ticks that actually happened, so there is
        # nothing to announce in advance.
        return frozenset()

    def unlock_holdout(self, reason: str) -> None:
        self._lock.unlock(reason)

    @property
    def holdout_lock(self) -> HoldoutLock:
        return self._lock

    def session_bounds(self, session) -> tuple[Ts, Ts]:
        d = self._as_date(session)
        key = d.toordinal()
        if key not in self._bounds_cache:
            self._load(d)
        return self._bounds_cache[key]

    # ---- loading ----------------------------------------------------------- #
    def load(self, session) -> list[Tick]:
        d = self._as_date(session)
        key = d.toordinal()
        if key not in self._tick_cache or key not in self._bounds_cache:
            self._load(d)
        return self._tick_cache[key]

    def _load(self, session: dt.date) -> None:
        path = self._path(session)
        if not path.exists():
            raise KeyError(f"no XAUUSD tick file for session {session} at {path}")
        df = pd.read_parquet(path)
        if df.empty:
            raise ValueError(f"session {session} holds an empty quote stream")

        # Mid-price, canonical. The bid/ask columns are dropped once the mid is
        # taken: the engine's `Tick` carries a single price.
        mid = (df["bid"].to_numpy(dtype=float) + df["ask"].to_numpy(dtype=float)) / 2.0
        ts = df["timestamp"].to_numpy(dtype="datetime64[ns]").astype("int64")
        key = session.toordinal()

        ticks = [
            Tick(ts=int(ts[i]), price=float(mid[i]), size=1, session=key, side=None)
            for i in range(len(df))
        ]
        self._tick_cache[key] = ticks
        self._bounds_cache[key] = (int(ts[0]), int(ts[-1]))

    def ticks(self, split: Split | None = None, *, warmup: int = 0) -> Iterator[Tick]:
        """Every tick of every session, in order."""
        if split is not None:
            self._lock.check(split)
            if self.spec.smoke_sessions is not None:
                if split is Split.HOLDOUT:
                    raise NotImplementedError("the smoke path never serves the holdout")
                for d in self._sessions[: self.spec.smoke_sessions]:
                    yield from self.load(d)
                return
            warm, live = self._split_positions(split, warmup)
            for i in list(warm) + list(live):
                yield from self.load(self._sessions[i])
            return
        for d in self._sessions:
            yield from self.load(d)
