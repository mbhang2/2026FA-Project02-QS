"""Volume at price for one session: VPOC, Value Area, Low Volume Nodes.

The Auction Market Theory primitives named in
`config/hypotheses/auction-market-features.md`, taken from ViperQ (arXiv
2609.13825). This module computes them; it takes no view on whether they predict
anything.

**Everything here is "so far in this session", and that is the whole point.**
A session's finished VPOC is not knowable while the session is running, so a
feature built from it is future data wearing a feature's clothes. `add()` is
incremental and every query answers from what has been added. A strategy that
wants the completed profile of *yesterday* keeps yesterday's object; it does not
get today's early.

**Determinism.** Ties are resolved toward the lower price and the rules are
stated at each method. Two runs over the same ticks produce the same numbers, on
any platform, because everything is integer bin arithmetic over `int` volumes --
no float accumulation, so no ordering sensitivity.

**Absence is `None`, never `0.0`.** An empty profile has no VPOC, and returning a
price of zero would be a fabricated feature -- the hazard section 2f of
`docs/design/ml-strategies.md` names for labels, in its feature form.
"""

from __future__ import annotations

from bisect import insort


class SessionProfile:
    """Volume binned by price, for one session, updated as trades arrive.

    `tick_size` is the instrument's minimum price increment -- 0.25 for NQ. Prices
    are binned to it, so a bin is one tick wide and `price / tick_size` rounds to
    the bin index. Anything finer invents resolution the instrument does not have;
    anything coarser is a choice the caller should make deliberately by passing a
    multiple.
    """

    __slots__ = ("tick_size", "_bins", "_total", "_keys")

    def __init__(self, tick_size: float) -> None:
        if tick_size <= 0:
            raise ValueError(f"tick_size must be positive, got {tick_size}")
        self.tick_size = float(tick_size)
        self._bins: dict[int, int] = {}
        self._keys: list[int] = []          # sorted, for the value-area walk
        self._total = 0

    # -- building ---------------------------------------------------------- #
    def add(self, price: float, size: int) -> None:
        """One trade. `size` is contracts, and must be positive."""
        if size <= 0:
            raise ValueError(f"size must be positive, got {size}")
        key = int(round(price / self.tick_size))
        if key not in self._bins:
            self._bins[key] = 0
            insort(self._keys, key)
        self._bins[key] += size
        self._total += size

    def add_bar(self, low: float, high: float, volume: int) -> None:
        """One bar's volume, spread evenly over the price levels it touched.

        **The construction registered by
        `config/hypotheses/auction-market-features.md`, and it lives here so
        every strategy uses the same one.** Two campaigns that each invented
        their own spreading rule would be computing different features under one
        name, and nothing downstream would notice.

        Ticks are exact and do not fit the budget -- measured in
        `docs/reviews/task67/`: 8,338 s for the 384-session split against a
        7,200 s budget, against 1,726 s for one-second bars, which reproduce the
        tick-built profile to a total variation distance of about 0.03. So the
        profile is fed bars, and a bar is an interval over which the true
        distribution of volume across its price levels is unknown.

        **Even is the honest default.** It assumes nothing about where inside the
        bar the volume fell, which is the only thing an OHLCV bar entitles you
        to assume. Weighting toward the close, or triangularly toward the
        typical price, would be a claim about intra-bar dynamics that this data
        does not support -- and at one second an NQ bar spans a price level or
        three, so the choice barely moves the profile anyway. At thirty seconds
        it would, which is the other reason the hypothesis registers 1s.

        The remainder from an uneven division goes to the lowest levels rather
        than being dropped, so `total_volume` still equals the volume that was
        actually traded.
        """
        if volume <= 0:
            raise ValueError(f"volume must be positive, got {volume}")
        if high < low:
            raise ValueError(f"high {high} is below low {low}")
        lo = int(round(low / self.tick_size))
        hi = int(round(high / self.tick_size))
        levels = hi - lo + 1
        share, remainder = divmod(volume, levels)
        for i, key in enumerate(range(lo, hi + 1)):
            v = share + (1 if i < remainder else 0)
            if v:
                self.add(self.price_of(key), v)

    def reset(self) -> None:
        """A new session. Call on the session boundary, not on the first bar of
        one -- a profile carried across sessions is a different feature and not
        this one."""
        self._bins.clear()
        self._keys.clear()
        self._total = 0

    # -- reading ----------------------------------------------------------- #
    @property
    def total_volume(self) -> int:
        return self._total

    @property
    def is_empty(self) -> bool:
        return self._total == 0

    def price_of(self, key: int) -> float:
        return key * self.tick_size

    def vpoc(self) -> float | None:
        """Volume Point of Control: the price that has traded the most volume.

        Ties go to the **lower** price. Arbitrary, but stated and deterministic,
        which is what matters -- an unstated tie-break is a silent source of
        run-to-run difference.
        """
        if self.is_empty:
            return None
        best_key = min(
            (k for k in self._keys),
            key=lambda k: (-self._bins[k], k),
        )
        return self.price_of(best_key)

    def value_area(self, fraction: float = 0.70) -> tuple[float, float] | None:
        """The price band holding `fraction` of the volume, grown out from VPOC.

        The Market Profile construction: start at the VPOC bin, repeatedly compare
        the two bins above the current band against the two below, take whichever
        pair holds more volume, and stop once the band covers `fraction` of total
        volume. Where fewer than two bins remain on a side, what is left is used.

        Returns `(low, high)` inclusive. A single-bin session returns that bin
        twice rather than a zero-width band at the wrong place.
        """
        if not 0 < fraction <= 1:
            raise ValueError(f"fraction must be in (0, 1], got {fraction}")
        if self.is_empty:
            return None

        keys = self._keys
        poc = min(keys, key=lambda k: (-self._bins[k], k))
        i = j = keys.index(poc)
        covered = self._bins[poc]
        target = self._total * fraction

        while covered < target and (i > 0 or j < len(keys) - 1):
            below = keys[max(0, i - 2):i]
            above = keys[j + 1:j + 3]
            vol_below = sum(self._bins[k] for k in below)
            vol_above = sum(self._bins[k] for k in above)
            # Ties go up, matching the tie-break in `vpoc` going down: together
            # they keep the band from oscillating on symmetric profiles.
            if above and vol_above >= vol_below:
                j += len(above)
                covered += vol_above
            elif below:
                i -= len(below)
                covered += vol_below
            else:
                break
        return self.price_of(keys[i]), self.price_of(keys[j])

    def low_volume_nodes(self, ratio: float = 0.25) -> tuple[float, ...]:
        """Prices the session has traded *through* rather than *at*.

        A bin qualifies when it holds less than `ratio` of the VPOC's volume and
        holds less than both its neighbours -- a local minimum, not merely a quiet
        bin, because the tails of any profile are quiet without being nodes.

        Only bins strictly inside the traded range are considered: the first and
        last have one neighbour and cannot be minima. Prices never traded are not
        bins at all and are not nodes -- a gap is not a node.
        """
        if not 0 <= ratio <= 1:
            raise ValueError(f"ratio must be in [0, 1], got {ratio}")
        if len(self._keys) < 3:
            return ()
        bins = self._bins
        peak = max(bins.values())
        cutoff = peak * ratio
        keys = self._keys
        out = []
        for idx in range(1, len(keys) - 1):
            k = keys[idx]
            v = bins[k]
            if v < cutoff and v < bins[keys[idx - 1]] and v < bins[keys[idx + 1]]:
                out.append(self.price_of(k))
        return tuple(out)

    # -- derived positions, the form a model wants -------------------------- #
    def distance_from_vpoc(self, price: float) -> float | None:
        """Signed, in ticks. Positive means above the VPOC."""
        poc = self.vpoc()
        if poc is None:
            return None
        return (price - poc) / self.tick_size

    def value_area_position(self, price: float,
                            fraction: float = 0.70) -> int | None:
        """-1 below the value area, 0 inside it, +1 above. `None` if empty.

        The categorical form ViperQ uses. Kept separate from the band itself
        because a model wants the position and a human reading a run wants the
        prices.
        """
        band = self.value_area(fraction)
        if band is None:
            return None
        low, high = band
        if price < low:
            return -1
        if price > high:
            return 1
        return 0
