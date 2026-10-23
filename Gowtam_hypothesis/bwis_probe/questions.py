"""100 arbitrary starting-point questions for the BWIS source-type probe.

Ten themes x ten questions. Questions are phrased as research asks (mechanism
families), the same grain the researcher's decompose step emits, so each can be
fed straight to retrieval + hypothesis generation.
"""

THEMES: dict[str, list[str]] = {
    "Momentum and trend": [
        "intraday momentum in gold futures",
        "time-series momentum across commodity futures",
        "trend following with moving average crossovers on gold",
        "opening range breakout in index futures",
        "first-hour return predicting last-hour return",
        "momentum crashes and volatility scaling",
        "52-week high anchoring effect in gold prices",
        "overnight versus intraday return decomposition in gold",
        "momentum in currency pairs at 5-minute frequency",
        "trend persistence after central bank announcements",
    ],
    "Mean reversion": [
        "intraday mean reversion after large gold price spikes",
        "Bollinger band reversion in FX at short horizons",
        "overnight gap fill probability in index futures",
        "pairs trading between gold and silver",
        "gold-silver ratio mean reversion",
        "short-term reversal after high volume bars",
        "Ornstein-Uhlenbeck calibration for intraday spreads",
        "VWAP reversion in futures markets",
        "RSI oversold bounce effectiveness in commodities",
        "reversal following liquidity shocks in futures",
    ],
    "Microstructure and order flow": [
        "order flow imbalance predicting short-term gold returns",
        "bid-ask spread widening as a predictor of volatility",
        "volume-synchronized probability of informed trading in futures",
        "tick-level price impact of aggressive orders",
        "quote stuffing and fleeting liquidity in futures",
        "intraday volume seasonality and returns",
        "market maker inventory effects on gold prices",
        "trade size clustering and information content",
        "limit order book depth and price discovery in futures",
        "latency arbitrage between gold futures and spot",
    ],
    "Volatility and risk": [
        "volatility clustering and GARCH forecasting for gold",
        "realized volatility signature plot and sampling frequency",
        "volatility risk premium in commodity options",
        "intraday volatility seasonality in gold",
        "volatility breakout strategies on 5-minute bars",
        "jump detection in high-frequency gold returns",
        "heavy tails and extreme value theory in intraday returns",
        "volatility targeting and risk parity overlays",
        "range-based volatility estimators Parkinson Garman-Klass",
        "implied versus realized volatility spread for gold",
    ],
    "Macro and event-driven": [
        "gold price reaction to US CPI releases",
        "FOMC announcement drift in gold",
        "non-farm payrolls surprise and intraday gold returns",
        "real interest rates as a driver of gold prices",
        "US dollar index and gold inverse relationship",
        "geopolitical risk index and gold safe-haven demand",
        "central bank gold purchases and price momentum",
        "inflation expectations breakeven rates and gold",
        "treasury yield curve slope and precious metals",
        "pre-announcement drift before macroeconomic news",
    ],
    "Seasonality and calendar": [
        "time-of-day effects in gold returns",
        "day-of-week seasonality in commodity futures",
        "turn-of-the-month effect in futures markets",
        "London fix price manipulation and intraday patterns",
        "session overlap London New York volatility and returns",
        "month-end rebalancing flows and futures returns",
        "holiday effects on gold and precious metals",
        "futures contract roll yield and calendar effects",
        "Asian session range as predictor of European session",
        "weekend gap behavior in gold",
    ],
    "Cross-asset and relative value": [
        "gold equity miners lead-lag relationship",
        "bitcoin and gold correlation regimes",
        "oil gold ratio and macro regime",
        "equity index futures leading gold intraday",
        "FX carry and commodity currency links to gold",
        "copper gold ratio as growth indicator",
        "VIX changes and gold short-term returns",
        "cross-sectional momentum in commodity futures",
        "term structure slope as a return predictor in commodities",
        "ETF flows GLD and futures price pressure",
    ],
    "Machine learning and signals": [
        "gradient boosting for short-horizon return prediction",
        "deep learning on limit order book data",
        "reinforcement learning for execution and trading",
        "feature importance stability in financial machine learning",
        "regime detection with hidden Markov models",
        "sentiment from news headlines predicting gold returns",
        "Twitter or social media sentiment and commodity returns",
        "transformer models for financial time series forecasting",
        "meta-labeling to filter trade signals",
        "online learning and concept drift in trading models",
    ],
    "Structural and auction dynamics": [
        "volume exhaustion after climactic price extensions",
        "intraday high-low range expansion and continuation",
        "price discovery around cash market open and close",
        "settlement period drift in commodity futures",
        "liquidity black holes during high volatility regime shifts",
        "consecutive up-down bar imbalance and mean reversion",
        "compression and expansion cycles in historical volatility",
        "volume spike absorption at key technical levels",
        "inventory rebalancing effects in futures rolls",
        "post-announcement volatility contraction and range trading",
    ],
    "Execution and portfolio construction": [
        "optimal execution Almgren-Chriss for futures",
        "position sizing with fractional Kelly criterion",
        "slippage estimation for gold futures at open",
        "stop-loss and take-profit placement effects",
        "volatility-adjusted position sizing for intraday strategies",
        "drawdown control and risk of ruin",
        "turnover constraints and net-of-cost alpha",
        "portfolio diversification across low-correlation strategies",
        "capacity and market impact of systematic gold strategies",
        "tail-risk hedging overlays for trend strategies",
    ],
}


def all_questions() -> list[tuple[str, str, str]]:
    """Flat list of (qid, theme, question), ids Q001..Q100."""
    out: list[tuple[str, str, str]] = []
    n = 0
    for theme, qs in THEMES.items():
        for q in qs:
            n += 1
            out.append((f"Q{n:03d}", theme, q))
    return out
