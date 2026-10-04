# A short-side opening reversal pattern exists in NQ futures

This campaign investigates whether a short-side reversal pattern, concentrated in the opening minutes of the session, exists in NQ futures. The idea is adapted from the paper doi-10.2139_ssrn.7124578, "Opening-Range Reversal in Dow Jones Futures: Evidence Consistent with 0DTE Hedging Pressure", which documented a similar effect in E-mini Dow futures.

## The claim
The market will tend to reverse its initial direction on some days. Specifically, if NQ futures establish a high early in the session, a strategy that sells this high with a fixed target and stop may earn more than it costs. The paper found this pattern on the short side in Dow futures, with a 59.79% win rate over 194 trades. The effect was strongest for trades entered in the earliest part of the session, which won 65.56% of the time.

## Why it might be true, and why it might not
The paper suggests the pattern could be driven by hedging pressure from dealers managing large, directional option exposures, particularly from zero-days-to-expiry (0DTE) options. If large market participants must buy or sell at the open regardless of price, their initial impact may create a temporary price dislocation that is subsequently corrected. This mechanism, if it exists, could plausibly affect the entire US index futures complex, including NQ.

However, the claim might not hold for NQ. NQ has a different composition (tech-heavy) and higher volatility than the Dow. These differences could mean that hedging flows are structured differently, or that any reversal tendency is overwhelmed by stronger directional momentum or noise. The paper also notes that a reconstructed historical sample for the Dow from approximately 2021-2022 showed a win rate of only 45.22%, suggesting the effect may be recent and time-dependent. Our data may fall into a period where the effect is weak or non-existent.

## What is yours to choose
The strategy agent may define the opening period, the specific entry conditions for a short trade (e.g., how a high is defined), and the parameters for a symmetric risk/reward structure (i.e., the size of the stop and target in ticks or points).

## What is NOT yours to choose
The strategy must be short-only. It must be a reversal strategy, meaning it sells after a price rise. The trade structure must use a symmetric 1:1 risk/reward target and stop.

## What would make the answer interesting
If a profitable short-side reversal pattern is found, it would suggest that the hedging-pressure dynamic described in the paper is a market-wide phenomenon affecting NQ as well as the Dow. If no such pattern is found, it would suggest the effect is either specific to the Dow, confined to a time period not covered by our data, or that the higher volatility of NQ makes such a simple reversal strategy unworkable.

## What this is not asking for
This campaign is not looking for a long-side (buy-the-dip) reversal pattern. It is also not testing strategies with asymmetric risk/reward ratios, or strategies that use indicators other than price action in the opening minutes to generate a signal.

## Why this one
This paper proposes a simple, specific, and timely market anomaly. Its hypothesis—that the explosion in 0DTE options volume has created predictable intraday patterns—is a major current topic of market discussion. By adapting the paper's claim to NQ, we can test the generality of this proposed effect on an instrument central to our work, moving beyond a single-instrument observation to a broader market question.
