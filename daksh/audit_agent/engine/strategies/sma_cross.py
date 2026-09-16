"""Dual moving-average cross (Brock, Lakonishok, LeBaron 1992 style).

Long when fast SMA > slow SMA, short when below. Window ends at t inclusive.
"""


def _sma(closes: list[float], end: int, window: int) -> float:
    start = end - window + 1
    chunk = closes[start : end + 1]
    return sum(chunk) / window


class SmaCross:
    name = "sma_cross"
    fast = 50
    slow = 200

    def signal(self, closes: list[float]) -> list[float]:
        n = len(closes)
        out = [0.0] * n
        slow = self.slow
        fast = self.fast
        if fast < 1 or slow < fast:
            raise ValueError("sma windows must satisfy 1 <= fast <= slow")
        for t in range(n):
            if t + 1 < slow:
                continue
            f = _sma(closes, t, fast)
            s = _sma(closes, t, slow)
            if f > s:
                out[t] = 1.0
            elif f < s:
                out[t] = -1.0
        return out
