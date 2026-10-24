"""Gap-aware session-level directional strategy, fitted on training bars.

A descendant of the best prior shape (#34): one low-turnover continuous +1/-1
position.  The difference here is the signal is a weighted combination of two
session-start returns:

    gap    = first bar open - previous session close
    early  = signal bar close - first bar open

Prior best used only `early`.  But the position is held across sessions, so the
overnight gap between the previous close and the first open is a real part of
the P&L the strategy experiences.  Fitting whether, and how, to include the gap
should therefore choose a signal that matches the actual holding period better
than using the early move alone.

The fit is a small exhaustive search over lookback k, sign convention, and gap
weighting modes (early only, gap only, gap+early).  The runtime keeps the same
proven decision rule: evaluate once per session at bar k, enter when flat, and
flip an existing position only on a signal above a fitted magnitude threshold.
No bracket is used, because the prior evidence strongly favours continuous
holding over being stopped out by noisy intrabar moves.
"""

from __future__ import annotations


class Strategy:
    """Fitted session-direction strategy with a gap-aware early-return signal."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        max_lookback: int = 8,
        flip_threshold_quantile: float = 0.25,
        min_samples: int = 10,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.flip_threshold_quantile = flip_threshold_quantile
        self.min_samples = min_samples

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._flip_tau = 0.0
        self._w_gap = 0.0
        self._w_early = 1.0
        self._drift_direction = 1

        # Runtime session state
        self._session: int | None = None
        self._bars_this_session = 0
        self._first_open: float | None = None
        self._prev_close: float | None = None
        self._last_close: float | None = None
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        try:
            self._fit(ctx)
        except Exception:
            # Never let a fitting error kill the run.  Fall back to the proven
            # early-move-only rule with a single-bar lookback.
            self._lookback = 1
            self._sign = 1
            self._flip_tau = 0.0
            self._w_gap = 0.0
            self._w_early = 1.0
            self._drift_direction = 1

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, and gap weighting from training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            return

        self._drift_direction = 1 if bars[-1].close >= bars[0].close else -1

        sessions: dict[int, list] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        sorted_sessions = sorted(sessions.keys())

        # Previous close for each session, used to compute the overnight gap.
        prev_close_by_sid: dict[int, float] = {}
        for i, sid in enumerate(sorted_sessions):
            if i == 0:
                prev_close_by_sid[sid] = sessions[sid][0].open
            else:
                prev_close_by_sid[sid] = sessions[sorted_sessions[i - 1]][-1].close

        # Weighting modes for (gap, early).  (0, 1) is the prior best shape;
        # (1, 1) is total return since the previous close; (1, 0) is gap only.
        modes = (
            (0.0, 1.0),
            (1.0, 1.0),
            (1.0, 0.0),
        )

        best_score = None
        best_key = None
        best_rows = None

        for k in range(1, self.max_lookback + 1):
            for w_gap, w_early in modes:
                rows: list[tuple[int, float, float]] = []
                for sid in sorted_sessions:
                    sb = sessions[sid]
                    if len(sb) < k:
                        continue
                    first_open = sb[0].open
                    prev_close = prev_close_by_sid[sid]
                    gap = first_open - prev_close
                    entry_close = sb[k - 1].close
                    early = entry_close - first_open
                    signal = w_gap * gap + w_early * early
                    if abs(signal) < 1e-12:
                        continue
                    rows.append((sid, signal, entry_close))

                if len(rows) < self.min_samples:
                    continue

                for sign in (1, -1):
                    score = self._eval_sign(rows, sessions, sorted_sessions[-1], sign)
                    key = (score, -len(rows))
                    if best_key is None or key > best_key:
                        best_score = score
                        best_key = key
                        best_lookback = k
                        best_sign = sign
                        best_rows = rows
                        best_mode = (w_gap, w_early)

        if best_rows is not None:
            self._lookback = best_lookback
            self._sign = best_sign
            self._w_gap, self._w_early = best_mode
            self._flip_tau = self._quantile_abs(
                best_rows, self.flip_threshold_quantile
            )

    def _eval_sign(self, rows, sessions, last_sid, sign) -> float:
        """Evaluate continuous holding P&L for a fixed sign convention.

        Rows are (session_id, signal, entry_close).  The position is assumed to
        fill at the signal bar's close and is then held until the next session's
        signal bar close, at which point it can be changed.  This mirrors the
        runtime's continuous exposure.
        """
        pnl = 0.0
        prev_desired = None
        prev_entry = None

        for _sid, signal, entry_close in rows:
            desired = 1 if sign * signal > 0 else -1

            if prev_desired is not None and prev_entry is not None:
                pnl += (entry_close - prev_entry) * prev_desired

            prev_desired = desired
            prev_entry = entry_close

        if prev_desired is not None and prev_entry is not None:
            last_close = sessions[last_sid][-1].close
            pnl += (last_close - prev_entry) * prev_desired

        return pnl

    @staticmethod
    def _quantile_abs(rows, q: float) -> float:
        values = sorted(abs(r[1]) for r in rows)
        if not values:
            return 0.0
        idx = int(q * len(values))
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _direction_for_signal(self, signal: float) -> int:
        if signal > 0:
            return self._sign
        return -self._sign

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the single market order needed to move to +1 or -1."""
        if desired == 0:
            return

        pos = ctx.position
        if desired > 0:
            if pos == 0:
                ctx.buy(size=self.size, tif="DAY", tag="session-long")
            elif pos < 0:
                ctx.buy(size=-pos + self.size, tif="DAY", tag="session-flip-long")
        else:
            if pos == 0:
                ctx.sell(size=self.size, tif="DAY", tag="session-short")
            elif pos > 0:
                ctx.sell(size=pos + self.size, tif="DAY", tag="session-flip-short")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # New session: reset per-session state.  The previous session's last
        # close is carried over as the anchor for the overnight gap.
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._first_open = None
            self._prev_close = self._last_close
            self._evaluated_this_session = False

        # First bar of the session establishes the open.  For the very first
        # session there is no previous close yet, so the gap is zero and the
        # signal reduces to the early-session move.
        if self._first_open is None:
            self._first_open = bar.open
        if self._prev_close is None:
            self._prev_close = bar.open

        self._bars_this_session += 1
        self._last_close = bar.close

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._evaluated_this_session:
            return
        if self._bars_this_session < self._lookback:
            return

        self._evaluated_this_session = True

        first_open = self._first_open
        prev_close = self._prev_close
        if first_open is None or prev_close is None:
            return

        gap = first_open - prev_close
        early = bar.close - first_open
        signal = self._w_gap * gap + self._w_early * early

        pos = ctx.position

        # Zero signal: if flat, establish drift exposure rather than wait
        # indefinitely; if already in a position, leave it alone.
        if abs(signal) < 1e-12:
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._direction_for_signal(signal)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current:
            # Flip only on a strong contrary signal; small signals are likely
            # noise and turnover is expensive.
            if abs(signal) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
