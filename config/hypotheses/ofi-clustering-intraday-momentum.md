# ofi-clustering-intraday-momentum

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Order flow imbalance (OFI) clusters in specific intraday market states, and when OFI persists within these states, subsequent 5-minute returns exhibit directional momentum due to slow mean reversion of price impact.

## The claim this makes testable

If intraday market states (detected via microstructure clustering) can be identified from 5-minute OFI features, then returns in the subsequent 5-minute bar will be positively correlated with the sign and magnitude of OFI in the current bar, conditional on state membership, more strongly than in non-clustered regimes.

## How to make it concrete

Construct OFI from 5-minute close prices and volume (buy/sell-initiated volume ratio or signed volume proxy); detect temporal market states using correlation clustering of OFI and volatility; regress forward returns on lagged OFI, state indicators, and interaction terms; measure predictive lift of state-conditional OFI coefficients relative to pooled model.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: High-frequency volume attribution (buy vs. sell volume per bar) or tick direction inference; market state labels from unsupervised clustering of intraday microstructure feature time series.

## Where it came from

- [Cross-Impact of Order Flow Imbalance in Equity Markets](http://arxiv.org/abs/2112.13213v4): Order flow imbalance integrated across multiple limit order book levels provides improved explanatory power for price impact compared to single-level OFI.
- [Stochastic Price Dynamics in Response to Order Flow Imbalance: Evidence from CSI 300 Index Futures](http://arxiv.org/abs/2505.17388v1): Price dynamics respond to order flow imbalance shocks via mean-reverting Ornstein-Uhlenbeck processes with memory, implying lagged impact structure.
- [Detecting intraday financial market states using temporal clustering](http://arxiv.org/abs/1508.04900v3): Temporal clustering of intraday market microstructure features enables detection of distinct market states with characteristic signatures.
- [Slow decay of impact in equity markets](http://arxiv.org/abs/1407.3390v1): Market impact of orders decays slowly over multiple days, consistent with autocorrelated order flow driving persistent price dynamics.

## Why it is worth a trial

Order flow imbalance is a direct observable proxy for market microstructure demand/supply; its clustering in distinct temporal regimes suggests regime-dependent momentum mechanics. The slow decay of impact and mean-reverting structure imply exploitable persistence within clustered states. Index futures offer deep liquidity and continuous trading, making OFI-based state detection feasible at 5-minute resolution without requiring tick-level data.
