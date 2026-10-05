# Standard OHLCV intraday momentum signals on five-minute bars do not generate enough gross edge to overcome execution costs.

This campaign investigates whether standard intraday momentum patterns can generate enough gross return to overcome execution costs. The idea comes from paper 2605.04004v3, which argues that in highly liquid index futures, simple bar-based directional patterns are arbitraged down to the friction floor.

## The claim
Simple OHLCV momentum signals—such as opening range breakouts, gap fills, and volume spikes—do not produce a gross edge larger than the cost of trading on five-minute bars. Any apparent directional move is either consumed within the breakout bar itself, or the gross return is simply too small (often between 0.07 and 1.50 points) to cover the spread, commission, and slippage. To be profitable, a strategy must rely on something other than single-bar directional predictions from publicly observable OHLCV features, or it must execute on a different time horizon.

## Why it might be true, and why it might not
It might be true because competitive markets quickly arbitrage away easily observable single-bar patterns. Faster participants with lower costs take the other side until the gross edge equals the friction cost of the marginal participant. For example, in breakout scenarios, the directional move often completes entirely within the breakout bar. By the time the bar closes and a systematic strategy enters, the momentum has exhausted, leaving only a reversal to capture.

It might not be true because specific structural conditions might still contain enough momentum to clear costs before reverting. For instance, gap continuations measured with sub-bar velocity (such as applying a Kalman filter to one-minute bars to estimate directional speed) might identify genuine institutional flow that persists long enough to overcome the friction ceiling.

## What is yours to choose
The agent may choose the specific momentum pattern to exploit (e.g., opening range breakout, gap continuation, volume exhaustion). You may set the lookback periods for defining the range or baseline, the bar interval (exploring sub-five-minute intervals to capture moves before they exhaust is highly encouraged), the velocity thresholds, and the stop and target logic. You may also determine how long to hold the position, whether for a single bar or an extended intraday period.

## What is NOT yours to choose
The instrument is fixed to NQ. The requirement to account for realistic execution costs is absolute; a strategy must earn more than it pays in commissions and slippage. The execution style remains event-driven at the bar close.

## What would make the answer interesting
If the strategy cannot overcome costs across various momentum setups, it confirms the structural limit of simple OHLCV patterns in liquid futures. If it does find a profitable rule, it maps the exact boundary where the friction ceiling breaks down, perhaps through better timing, shorter bar intervals, or specific structural filters like gap velocity.

## What this is not asking for
We are not asking for complex multi-feature regime detection models or Markov transition matrices. We are testing the limits of standard OHLCV momentum and structural price patterns.

## Why this one
Paper 2605.04004v3 shifts the focus from finding the perfect indicator to measuring the structural ceiling of gross edge. It forces us to ask whether our standard momentum setups are fundamentally capped by market friction, changing how we view the viability of simple bar-close execution in liquid index futures.
