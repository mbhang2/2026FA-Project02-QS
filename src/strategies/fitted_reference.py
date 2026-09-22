"""The calibration reference for fitted strategies. Not a candidate.

Deliberately the dumbest thing that is genuinely *fitted*: it accumulates one
feature over the fit window, fits a single threshold to it, and trades that
threshold afterwards. There is no model library, no training loop and no
hyperparameter, because none of those are what needs proving.

What needs proving is the chain: that `fit()` is called once at the edge of the fit
window on data the strategy accumulated itself, that the weights survive a round
trip through a file, that the scored window excludes the fit window, and that a
later run loads the weights instead of refitting. A logistic regression would test
exactly the same chain and make the failures harder to read.

See `docs/design/ml-strategies.md`.
"""

from __future__ import annotations


class FittedReference:
    """Trades the sign of delta against a threshold fitted to the fit window."""

    def __init__(self, *, interval: str = "5m", stop: float = 4.0,
                 target: float = 4.0, size: int = 1):
        self.interval = interval
        self.stop = stop
        self.target = target
        self.size = size
        # Accumulated during the fit window, one value per closed bar.
        self._seen: list[int] = []
        # The fitted parameter. `None` means "not fitted yet", and the trading
        # path checks it rather than assuming: a strategy that traded on an
        # unfitted threshold would silently be a different strategy.
        self.threshold: float | None = None

    # ---- the ordinary strategy contract ----------------------------------- #

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        history = ctx.history(self.interval)
        if not history:
            return
        bar = history[-1]

        # Accumulation happens on every bar, fit window included. The engine
        # ignores actions there, so this is the only work being done.
        self._seen.append(abs(bar.delta))

        if self.threshold is None:
            return                      # still accumulating; cannot trade yet
        if ctx.position or ctx.working_order_ids:
            return
        if abs(bar.delta) < self.threshold:
            return
        place = ctx.buy if bar.delta > 0 else ctx.sell
        place(size=self.size, stop_offset=self.stop,
              target_offset=self.target, tag="fitted")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    # ---- the fitted contract ----------------------------------------------- #

    def fit(self) -> None:
        """One parameter, from what was accumulated. Called once, by the engine.

        The 80th percentile of absolute delta over the fit window: "large" defined
        by what this instrument actually did, rather than by a constant somebody
        chose. It is a frozen statistic of the fit window, which is the legitimate
        case in section 2c -- computed once, applied unchanged everywhere after.
        """
        if not self._seen:
            raise RuntimeError(
                "fit() was called with nothing accumulated. The engine calls it at "
                "the edge of the fit window; reaching it empty means the window was "
                "not walked.")
        ordered = sorted(self._seen)
        self.threshold = float(ordered[int(0.80 * (len(ordered) - 1))])

    def save_weights(self) -> bytes:
        """Hand the parameters over; the harness decides where they live.

        Bytes rather than a path because this object runs in a child process
        that is denied `file-write*`. A strategy cannot persist itself, and the
        API should not pretend otherwise.
        """
        return f"{self.threshold!r}\n".encode()

    def load_weights(self, blob: bytes) -> None:
        self.threshold = float(blob.decode().strip())
