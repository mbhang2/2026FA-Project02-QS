"""Always-long control. On a random walk, Sharpe should be near zero after costs."""


class BuyHold:
    name = "buy_hold"

    def signal(self, closes: list[float]) -> list[float]:
        return [1.0] * len(closes)
