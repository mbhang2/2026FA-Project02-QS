import pandas as pd
import numpy as np

def _get_hlc(prices_df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    if 'high' in prices_df.columns and 'low' in prices_df.columns and 'close' in prices_df.columns:
        return prices_df['high'], prices_df['low'], prices_df['close']
    c = prices_df['close'] if 'close' in prices_df.columns else (prices_df['bid'] + prices_df['ask']) / 2
    return c, c, c

def _compute_macd_signals(prices: pd.Series, lookbacks: list[int]) -> dict[str, pd.Series]:
    signals = {}
    for short_lb in lookbacks:
        for long_lb in lookbacks:
            if short_lb >= long_lb: continue
            ema_short = prices.ewm(span=short_lb, adjust=False).mean()
            ema_long = prices.ewm(span=long_lb, adjust=False).mean()
            macd_line = ema_short - ema_long
            for sig_lb in lookbacks:
                signal_line = macd_line.ewm(span=sig_lb, adjust=False).mean()
                sig = pd.Series(0.0, index=prices.index)
                sig[macd_line > signal_line] = 1.0
                sig[macd_line < signal_line] = -1.0
                sig.iloc[:(long_lb + sig_lb - 2)] = np.nan
                signals[f"MACD_{short_lb}_{long_lb}_{sig_lb}"] = sig
    return signals

# Create dummy data
np.random.seed(42)
prices = pd.Series(np.cumsum(np.random.randn(100)) + 100, index=pd.date_range('2024-01-01', periods=100))
prices_df = pd.DataFrame({'close': prices, 'high': prices + 1, 'low': prices - 1})

macd = _compute_macd_signals(prices_df['close'], [5, 10, 20])
print("MACD length:", len(macd))

def _compute_rsi_signals(prices: pd.Series, lookbacks: list[int]) -> dict[str, pd.Series]:
    signals = {}
    for lb in lookbacks:
        delta = prices.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        
        # Standard RSI smoothing: Wilders
        avg_gain = gain.ewm(alpha=1/lb, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/lb, adjust=False).mean()
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        # For an expert system, RSI signals typically cross overbought/oversold levels
        # E.g., > 70 is overbought (sell), < 30 is oversold (buy)
        # However, momentum-based RSI experts might go long if > 50.
        # We will implement > 50 as 1 and < 50 as -1 based on MACD/typical rules, or crossovers of 70/30.
        # The paper typically uses overbought/oversold levels or simply centerline. Let's provide 30/70 crossovers:
        # Cross above 30 -> +1 (bullish), Cross below 70 -> -1 (bearish)
        # Or centerline: RSI > 50 -> +1, RSI < 50 -> -1. Let's do centerline for simplicity if not specified.
        # Wait, literature often uses 30/70. Let's stick to 50 for momentum as it's binary (-1, 1).
        
        # We'll use 50 crossing to keep binary [-1, 1] without having to wait for 30/70. Or perhaps both.
        pass
