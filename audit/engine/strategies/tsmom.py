"""Time-series momentum (Moskowitz, Ooi, Pedersen 2012), single-series port.

position_t = sign(close_t / close_{t-lookback} - 1). Uses closes[0:t+1] only.
"""


class TsMom:
    name = "tsmom"
    lookback = 252

    def signal(self, closes: list[float]) -> list[float]:
        n = len(closes)
        lb = self.lookback
        out = [0.0] * n
        for t in range(n):
            src = t - lb
            if src < 0:
                continue
            prev = closes[src]
            if prev == 0:
                continue
            ret = closes[t] / prev - 1.0
            if ret > 0:
                out[t] = 1.0
            elif ret < 0:
                out[t] = -1.0
        return out
