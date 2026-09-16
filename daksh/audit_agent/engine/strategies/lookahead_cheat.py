"""Deliberate look-ahead: sign of *this bar's* close-to-close return.

Exists so the validator has a known FAIL case. Never dispatch this as research.
"""


class LookaheadCheat:
    name = "lookahead_cheat"

    def signal(self, closes: list[float]) -> list[float]:
        out = [0.0]
        for i in range(1, len(closes)):
            ret = 0.0 if closes[i - 1] == 0 else closes[i] / closes[i - 1] - 1.0
            out.append(1.0 if ret > 0 else (-1.0 if ret < 0 else 0.0))
        return out
