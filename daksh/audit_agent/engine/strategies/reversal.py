"""Reversal ports: short-horizon (Lehmann 1990) and long-horizon (De Bondt & Thaler 1985).

Each signal is the opposite of the lookback return ending at t. PIT: closes[0:t+1] only.
"""


def _sign_flip_return(closes: list[float], t: int, lookback: int) -> float:
    src = t - lookback
    if src < 0:
        return 0.0
    prev = closes[src]
    if prev == 0:
        return 0.0
    ret = closes[t] / prev - 1.0
    if ret > 0:
        return -1.0
    if ret < 0:
        return 1.0
    return 0.0


class ShortReversal:
    name = "short_reversal"
    lookback = 5

    def signal(self, closes: list[float]) -> list[float]:
        return [_sign_flip_return(closes, t, self.lookback) for t in range(len(closes))]


class LongReversal:
    name = "long_reversal"
    lookback = 504

    def signal(self, closes: list[float]) -> list[float]:
        return [_sign_flip_return(closes, t, self.lookback) for t in range(len(closes))]
