# hypothesis.md
# Learning the Dynamics of Technical Trading Strategies — Implementation Hypothesis

## 0. Purpose

Implement a reproducible, daily-bar version of:

> N. J. Murphy and T. Gebbie, **“Learning the dynamics of technical trading strategies”**, arXiv:1903.02228v3, 2019.

The implementation should reproduce the **daily-data methodology** of the paper as closely as possible, while explicitly documenting adaptations required by a constrained dataset of approximately one year of daily OHLCV data.

The paper's central idea is **not** to select one historically best technical rule. Instead, it creates a population of technical-trading "experts", lets those experts generate buy/sell/hold signals, converts those signals into self-financing zero-cost portfolio controls, and uses an online learning/meta-learning procedure to weight experts according to accumulated wealth. The resulting aggregate portfolio is then evaluated before and after transaction costs, against a benchmark, and using statistical-arbitrage and back-test-overfitting diagnostics.

The paper explicitly states that its objective is to study the collective dynamics of a population of technical strategies rather than identify a single profitable technical indicator. [Source: Appendix/Introduction.]

---

# 1. Core Research Hypothesis

## Primary hypothesis

**H1 — Adaptive aggregation hypothesis**

A performance-weighted online aggregation of a diverse population of technical-trading experts can generate a portfolio whose cumulative wealth and risk-adjusted performance are superior to, or at least competitive with, a passive/benchmark portfolio over the evaluation period.

Formally, let:

- `H_t` = matrix of expert portfolio controls at time `t`
- `q_t` = normalized expert mixture weights
- `b_t` = aggregate portfolio control
- `x_t` = vector of asset price relatives

Then:

`b_{t+1} = Σ_n q_{n,t+1} h^n_t`

and the aggregate wealth evolves according to the realized next-period asset price relatives.

The hypothesis is supported only if the aggregate strategy demonstrates economically meaningful performance **out of sample and after realistic trading costs**. A profitable gross backtest alone is not sufficient.

The paper's objective is specifically to examine aggregate expert dynamics, statistical arbitrage, and generalization/back-test overfitting rather than to assert that technical analysis itself necessarily contains exploitable information.

---

# 2. Secondary Research Hypotheses

## H2 — Expert differentiation

Different technical-trading rules and parameterizations will produce materially different wealth paths and risk profiles.

Expected observable implication:

- terminal expert wealth will vary across experts;
- expert rankings will vary over time;
- no single rule should be assumed ex ante to dominate;
- strategy families may exhibit different distributions of returns and wealth.

---

## H3 — Adaptive selection / aggregation

Experts that accumulate greater wealth should receive greater influence in subsequent aggregate portfolio construction.

Expected observable implication:

- expert mixture weights should evolve over time;
- experts with stronger recent/accumulated performance should contribute more heavily;
- the aggregate portfolio should adapt rather than maintain fixed weights across experts.

---

## H4 — Transaction-cost robustness

A strategy that appears profitable before costs may lose much or all of its economic performance after transaction costs.

Therefore:

1. calculate gross performance;
2. calculate performance after transaction costs;
3. compare the difference;
4. report turnover/trading activity.

A strategy should **not** be considered robust solely because its gross equity curve is positive.

---

## H5 — Statistical-arbitrage hypothesis

The aggregate portfolio may exhibit properties consistent with statistical arbitrage before transaction costs, but evidence should weaken or disappear after realistic daily trading costs unless the strategy genuinely survives market frictions.

The paper defines statistical arbitrage using four asymptotic conditions:

1. zero initial cost;
2. positive expected discounted cumulative profit in the limit;
3. probability of loss converging to zero;
4. conditional variance of negative incremental profits converging to zero.

The empirical implementation uses the Jarrow et al. (2012) Min-t framework.

---

## H6 — Back-test-overfitting hypothesis

Because the model evaluates a large population of strategies/parameter combinations, apparent performance can arise from selection effects and historical path dependence.

The implementation therefore attempts to estimate the **Probability of Back-test Overfitting (PBO)** using the combinatorially symmetric cross-validation (CSCV) procedure of Bailey et al. (2016).

A low PBO is evidence that the observed strategy-selection performance is less likely to be explained by back-test overfitting; it is not proof of future profitability.

---

# 3. Data Contract

## Required daily fields

The minimum input schema is:

```text
date
asset_id
open
high
low
close
volume
```

Optional:

```text
adjusted_close
risk_free
sector
```

The implementation must operate on **chronologically ordered daily observations**.

---

## Data assumptions

For every asset:

- one row represents one trading day;
- dates are strictly ordered;
- duplicate `(asset_id, date)` rows are forbidden;
- OHLC prices are positive;
- volume is non-negative;
- missing values must be explicitly handled;
- no future observations may enter any feature, signal, liquidity estimate, volatility estimate, or portfolio decision.

If adjusted prices are used, the choice must be documented and applied consistently.

---

# 4. Time Index and No-Lookahead Rule

This is a critical implementation requirement.

At the end of day `t`, the algorithm may use information observable from:

```text
OHLCV through day t
```

It may then calculate the portfolio to be held during:

```text
t -> t+1
```

The realized return for that position is obtained from:

```text
close[t] -> close[t+1]
```

Therefore:

```text
signal[t]
       ↓
portfolio[t+1]
       ↓
realized return[t+1]
```

Never:

```text
signal[t]
       ↓
return[t]
```

when the signal itself uses `close[t]`.

The implementation should make this temporal alignment explicit in the dataset.

Recommended columns:

```text
signal_date
decision_date
execution_period
realized_return_period
```

---

# 5. Price Relatives

For asset `m`:

`x_{m,t} = P^c_{m,t} / P^c_{m,t-1}`

where:

- `P^c_{m,t}` = closing price of asset `m` at time `t`.

The corresponding simple return is:

`r_{m,t} = x_{m,t} - 1`

The expert and aggregate portfolio returns are calculated from these price relatives.

For a portfolio control vector `b_t`:

`ΔS_t = 1 + Σ_m b_{m,t-1}(x_{m,t} - 1)`

and:

`S_t = S_{t-1} ΔS_t`

The cumulative profit/loss series is:

`PL_t = PL_{t-1} + (ΔS_t - 1)`

The crucial alignment is that controls established using information at `t-1` earn the return realized from `t-1` to `t`.

---

# 6. Investment Universe

The paper uses the most liquid stocks and uses Average Daily Volume (ADV) as a liquidity proxy.

For asset `m`:

`ADV_{m,t} = (1/δ_liq) Σ_{j=0}^{δ_liq-1} Volume_{m,t-j}`

The assets with the largest ADV are selected.

## Original-paper configuration

The paper uses:

- 15 most liquid JSE stocks;
- liquidity ranking based on historical ADV;
- daily implementation;
- six-year daily backtest.

## One-year-data adaptation

If only one year of daily data is available, the exact six-year replication is impossible.

Use one of these explicitly declared modes:

### Mode A — strict data-constrained replication

Use the entire available asset universe if it is already fixed and supplied by the dataset.

### Mode B — liquidity universe

Select the top `M` assets by trailing ADV using only information available before each decision.

Recommended:

```text
M = min(15, number_of_assets_available)
δ_liq = 90 trading days
```

If fewer than 90 historical days exist, either:

- delay activation until 90 observations exist; or
- use a documented expanding-window alternative.

For a strict replication, prefer the first option.

**Do not rank assets using full-sample volume**, because that uses future information.

---

# 7. Expert Definition

Each expert is defined by a combination of:

1. technical trading rule `ω`;
2. object/asset cluster `c`;
3. short lookback parameter `n1`;
4. long lookback parameter `n2`, where required.

The paper describes each combination as a separate expert.

Let:

- `W1` = number of one-parameter rules;
- `W2` = number of two-parameter rules;
- `C` = number of asset clusters;
- `L` = number of short lookback parameters;
- `K` = number of long lookback parameters.

For one-parameter rules:

`Ω_1 = C × L × W1`

For two-parameter rules, only combinations satisfying:

`n1 < n2`

are valid.

The total expert population is the sum of valid one-parameter and two-parameter combinations.

---

# 8. Cluster Structure

The paper uses four object clusters:

1. all stocks;
2. Resources;
3. Industrials;
4. Financials.

For a general dataset where sector labels are unavailable:

### Strict mode

Use only the trivial cluster containing all assets.

### Sector-aware mode

Use sector labels if they are part of the dataset and are known without lookahead.

### Experimental extension

If sectors are unavailable but the pipeline requires multiple clusters, clustering may be added only as a separately labeled extension. It must not be presented as exact replication of the paper.

The safest one-year replication is:

```text
C = 1
cluster = ALL_ASSETS
```

This avoids introducing an undocumented clustering model.

---

# 9. Technical Indicators

The paper's technical-rule population contains the following 13 technical rules plus three online portfolio-selection rules and one additional online rule family, yielding the strategy families reported in the paper.

Technical families:

1. Moving Average Crossover
2. Exponential Moving Average Crossover
3. Ichimoku Kijun Sen Cross
4. Momentum
5. Acceleration
6. MACD
7. Fast Stochastics
8. Slow Stochastics
9. RSI
10. MARSI
11. Bollinger Bands
12. Price Rate of Change (PROC)
13. Williams %R
14. Parabolic SAR

The paper also includes:

15. Zero-cost BCRP
16. Zero-cost Anti-BCRP
17. Zero-cost Anti-Correlation (Z-Anticor)

These should be implemented as separate expert families.

---

# 10. Simple Moving Average

For closing price `P^c_t`:

`SMA_t(n) = (1/n) Σ_{i=0}^{n-1} P^c_{t-i}`

The current-day value includes the current close.

It is therefore available only **after day t closes** and must be used for positions beginning after that observation.

---

# 11. Exponential Moving Average

The EMA is:

`EMA_t(n) = α P^c_t + (1-α) EMA_{t-1}(n)`

where:

`α = 2/(n+1)`

Initialization:

`EMA_0(n) = SMA(P^c,n)`

The implementation must use a deterministic initialization and must not use future observations.

---

# 12. Highest High / Lowest Low

For lookback `n`:

`HH_t(n) = max(High_{t-n+1}, ..., High_t)`

`LL_t(n) = min(Low_{t-n+1}, ..., Low_t)`

These are used by Ichimoku, stochastic oscillators, Bollinger-related rules, Williams %R, and other rules.

---

# 13. Ichimoku Kijun-Sen

The paper fixes:

```text
n1 = 7
```

and varies the longer parameters.

Kijun-Sen:

`Kijun_t(n2) = [HH_t(n2) + LL_t(n2)] / 2`

Trading rule:

### Buy

The Kijun-Sen crosses the closing-price curve from below to above / equivalently the closing price crosses the Kijun-Sen upward according to the paper's stated rule.

### Sell

The Kijun-Sen crosses the closing-price curve from above to below.

### Hold

Otherwise.

Implementation should encode crossings using previous-day and current-day relationships rather than a visual chart interpretation.

---

# 14. Momentum (MOM)

Momentum:

`MOM_t(n) = P^c_t - P^c_{t-n}`

The paper's momentum trading rule compares momentum with an EMA of momentum.

### Buy

`MOM_{t-1}(n) <= EMA_t(MOM(n), λ)`

and

`MOM_t(n) > EMA_t(MOM(n), λ)`

### Sell

`MOM_{t-1}(n) >= EMA_t(MOM(n), λ)`

and

`MOM_t(n) < EMA_t(MOM(n), λ)`

### Hold

Otherwise.

`λ` must be implemented exactly as specified by the selected implementation configuration. If the paper/supplement does not provide a value in the extracted source, do not invent one; expose it as a configuration parameter.

---

# 15. Acceleration (ACC)

Acceleration measures change in momentum:

`ACC_t(n) = MOM_t(n) - MOM_{t-1}(n)`

The paper's signal rule uses a shifted zero threshold:

### Buy

`ACC_{t-1}(n) + 1 <= 0`

and

`ACC_t(n) + 1 > 0`

### Sell

`ACC_{t-1}(n) + 1 >= 0`

and

`ACC_t(n) + 1 < 0`

### Hold

Otherwise.

This unusual `+1` must not be silently replaced with zero if the goal is strict paper replication.

---

# 16. MACD

Long EMA:

`EMA^L_t = EMA_t(P^c,n2)`

Short EMA:

`EMA^S_t = EMA_t(P^c,n1)`

MACD:

`MACD_t(n1,n2) = EMA^S_t - EMA^L_t`

Signal line:

`SL_t(n1,n2,n3) = EMA_t(MACD(n1,n2), n3)`

MACD signal:

`MACDS_t = MACD_t - SL_t`

### Buy

Current MACD crosses above the signal line.

### Sell

Current MACD crosses below the signal line.

### Hold

Otherwise.

The paper requires the short lookback to be smaller than the long lookback.

---

# 17. Fast Stochastic Oscillator

Fast %K:

`Fast%K_t(n) = (Close_t - LL_t(n)) / (HH_t(n) - LL_t(n))`

Fast %D:

`Fast%D_t(n) = SMA_t(Fast%K(n), 3)`

### Buy

`Fast%K_{t-1} <= Fast%D_t`

and

`Fast%K_t > Fast%D_t`

### Sell

`Fast%K_{t-1} >= Fast%D_t`

and

`Fast%K_t < Fast%D_t`

### Hold

Otherwise.

If `HH = LL`, the oscillator is undefined and the signal must be treated as unavailable/hold rather than producing an artificial infinite value.

---

# 18. Slow Stochastic Oscillator

Slow %K:

`Slow%K_t(n) = SMA_t(Fast%K(n),3)`

Slow %D:

`Slow%D_t(n) = SMA_t(Slow%K(n),3)`

### Buy

`Slow%K_{t-1} <= Slow%D_t(3)`

and

`Slow%K_t > Slow%D_t(3)`

### Sell

`Slow%K_{t-1} >= Slow%D_t(3)`

and

`Slow%K_t < Slow%D_t(3)`

### Hold

Otherwise.

---

# 19. RSI

The paper defines up and down closing-price observations and calculates RSI from moving averages of these components.

Conceptually:

- upward observation when `Close_t > Close_{t-1}`;
- downward observation when `Close_t < Close_{t-1}`;
- equal moves are neither up nor down under the paper's definition.

RSI:

`RSI_t(n) = 100 - 100/[1 + SMA(up,n)/SMA(down,n)]`

The implementation must handle zero denominators explicitly.

### Buy

`RSI_{t-1} <= 30`

and

`RSI_t > 30`

### Sell

`RSI_{t-1} >= 70`

and

`RSI_t < 70`

### Hold

Otherwise.

---

# 20. MARSI

MARSI is a moving-average-smoothed RSI:

`MARSI_t(n1,n2) = SMA_t(RSI(n1), n2)`

### Buy

`MARSI_{t-1} <= 30`

and

`MARSI_t > 30`

### Sell

`MARSI_{t-1} >= 70`

and

`MARSI_t < 70`

### Hold

Otherwise.

---

# 21. Bollinger Bands

Median band:

`Boll^m_t(n) = SMA_t(Close,n)`

Upper band:

`Boll^u_t(n) = Boll^m_t(n) + 2σ_t(n)`

Lower band:

`Boll^d_t(n) = Boll^m_t(n) - 2σ_t(n)`

The paper sets:

```text
s = 2
```

### Buy

`Close_{t-1} >= LowerBand_t`

and

`Close_t >= UpperBand_t`

### Sell

`Close_{t-1} <= LowerBand_t`

and

`Close_t > UpperBand_t`

### Hold

Otherwise.

Use the paper's exact inequalities when strict replication is required.

---

# 22. Price Rate of Change (PROC)

`PROC_t(n) = 100 × (Close_t - Close_{t-n}) / Close_{t-n}`

### Buy

`PROC_{t-1}(n) <= 0`

and

`PROC_t(n) > 0`

### Sell

`PROC_{t-1}(n) >= 0`

and

`PROC_t(n) < 0`

### Hold

Otherwise.

---

# 23. Williams %R

`Williams_t(n) = -100 × [HH_t(n)-Close_t] / [HH_t(n)-LL_t(n)]`

### Buy

`Williams_{t-1} >= -20`

and

`Williams_t < -80`

### Sell

`Williams_{t-1} <= -20`

and

`Williams_t > -80`

### Hold

Otherwise.

This rule must be implemented exactly as stated even though its threshold crossing direction may appear unusual relative to common textbook descriptions.

---

# 24. Parabolic SAR

The paper specifies:

```text
initial trend = up
AF0 = 0.02
AFmax = 0.20
```

The SAR update is:

`SAR_{t+1} = SAR_t + AF(EP - SAR_t)`

where:

- `EP` = extreme price;
- `AF` = acceleration factor.

The acceleration factor increases when a new extreme price is reached and is capped at `0.2`.

### Initialization

The paper initializes:

- trend = up;
- EP = 0;
- AF = 0.02;
- SAR = initial closing price;
- last high = initial high;
- last low = initial low.

The implementation must preserve deterministic state across dates.

### Buy

Previous SAR is at/above previous close and current SAR is below current close:

`SAR_{t-1} >= Close_{t-1}`

and

`SAR_t < Close_t`

### Sell

Previous SAR is at/below previous close and current SAR is above current close:

`SAR_{t-1} <= Close_{t-1}`

and

`SAR_t > Close_t`

### Hold

Otherwise.

---

# 25. Signal Representation

Each expert produces one signal per asset per date:

```text
+1 = buy / long
 0 = hold
-1 = sell / short
```

Important:

`0` means **hold the previous directional position** when the paper's signal-to-position transformation is applied. It does not automatically mean "close the position".

The implementation therefore needs to distinguish:

```text
raw_signal
position_signal
portfolio_weight
```

---

# 26. Hold-State Persistence

At time `t`, if the current signal is hold (`0`), replace it with the corresponding non-zero signal from `t-1`.

Thus:

```text
current signal = +1 -> +1
current signal = -1 -> -1
current signal =  0 -> previous non-zero position
```

This preserves the expert's existing directional position.

A newly generated non-zero signal overrides the previous signal.

Example:

```text
t-1: +1
t:    0
=> output signal at t = +1

t-1: -1
t:   +1
=> output signal at t = +1
```

The hold operation retains the **direction**, not the previous weight magnitude.

---

# 27. Four Signal Cases

After applying hold persistence, classify the expert's output signal vector.

## Case I — all zero

No active positions.

All stock controls:

`w_i = 0`

Risk-free control:

`w_rf = 0`

---

## Case II — only non-negative signals

The expert has long signals and/or zero signals.

Long assets receive positive weights.

The risk-free asset receives:

`w_rf = -0.5`

The long stock weights sum to:

`Σ_i w_i = +0.5`

Therefore:

`Σ_all assets w_i = 0`

and:

`Σ_all assets |w_i| = 1`

---

## Case III — only non-positive signals

Short assets receive negative weights.

The risk-free asset receives:

`w_rf = +0.5`

The short stock weights sum to:

`Σ_i w_i = -0.5`

Therefore:

`Σ_all assets w_i = 0`

and:

`Σ_all assets |w_i| = 1`

---

## Case IV — mixed long and short signals

Long assets receive positive weights.

Short assets receive negative weights.

The risk-free weight is chosen so that:

`Σ_all assets w_i = 0`

and the total absolute leverage is one.

Assets with hold/zero signals receive zero stock weight.

---

# 28. Volatility-Loading Weight Formula

For active assets, calculate historical volatility using closing prices.

For daily trading the paper uses a 90-day lookback.

The paper's daily volatility estimate is the standard deviation of closing prices over the previous 90 days.

For an active set with volatility vector `σ`:

`w_i = (1/2) σ_i / Σ_j σ_j`

for long assets, with the corresponding negative form for short assets.

Thus:

- more volatile active assets receive larger absolute weights under the paper's **volatility loading** approach;
- total long allocation is normalized to `+0.5`;
- total short allocation is normalized to `-0.5` where applicable.

The paper also discusses inverse-volatility loading but does not use it in its reported study because the results were similar.

For strict replication:

```text
use volatility loading
do not use inverse volatility loading
```

---

# 29. Risk-Free Asset

The original paper uses the STeFI short-term fixed-interest index as the risk-free asset.

If the supplied dataset has no risk-free asset:

### Replication adaptation

Use a synthetic cash/risk-free series only as a clearly documented implementation extension.

If the objective is to preserve the exact zero-cost mechanics, the risk-free control is primarily used to balance long-only or short-only signal cases.

Do not silently substitute a real-world risk-free rate and claim exact replication.

For a pure implementation test where no risk-free data exist, the cleanest option is:

```text
risk_free_return = 0
```

with this adaptation explicitly recorded.

---

# 30. Expert Control Matrix

For `Ω` experts and `M` assets:

`H_t = [h^1_t, h^2_t, ..., h^Ω_t]`

where each:

`h^n_t`

contains:

```text
M stock controls + 1 risk-free control
```

Therefore:

```text
H_t shape = (M+1) × Ω
```

Every expert must satisfy, when active:

`Σ_i h_i = 0`

and:

`Σ_i |h_i| = 1`

The all-zero expert is the explicit exception.

---

# 31. Expert Wealth

Initialize every expert:

`S^h_{n,0} = 1`

For expert `n` at time `t`:

`ΔS^h_{n,t} = 1 + Σ_m h^n_{m,t-1}(x_{m,t}-1)`

Then:

`S^h_{n,t} = S^h_{n,t-1} × ΔS^h_{n,t}`

The expert's wealth is therefore entirely determined by:

1. its previous portfolio controls;
2. realized asset price relatives.

---

# 32. Online Learning / Meta-Learning Algorithm

The online learner aggregates expert strategies according to accumulated expert wealth.

At each date:

1. update aggregate portfolio wealth using the portfolio held from the previous period;
2. update each expert's wealth;
3. use expert wealth as mixture scores;
4. normalize mixture scores;
5. combine expert controls;
6. obtain the portfolio for the next period.

This is the core adaptive component.

---

# 33. Step 1 — Update Aggregate Wealth

Given aggregate portfolio controls `b_{t-1}`:

`ΔS_t = 1 + Σ_m b_{m,t-1}(x_{m,t}-1)`

Then:

`S_t = S_{t-1} ΔS_t`

and:

`PL_t = PL_{t-1} + (ΔS_t - 1)`

---

# 34. Step 2 — Update Expert Wealth

For every expert `n`:

`ΔS^h_{n,t} = 1 + Σ_m h^n_{m,t}(x_{m,t}-1)`

and:

`S^h_{n,t} = S^h_{n,t-1} ΔS^h_{n,t}`

The exact temporal implementation must ensure that controls used to earn a return were generated before the corresponding return was realized.

---

# 35. Step 3 — Expert Mixture

The raw mixture value is:

`q_{n,t+1} = S^h_{n,t}`

Thus accumulated expert wealth acts as the performance score.

This is the mechanism by which successful experts receive greater influence.

---

# 36. Step 4 — Zero-Cost / Unit-Leverage Normalization

The paper normalizes the expert mixture to satisfy:

`Σ_n q_n = 0`

and:

`Σ_n |q_n| = 1`

The centering operation is:

`q'_n = q_n - (1/Ω) Σ_j q_j`

Then:

`q''_n = q'_n / Σ_j |q'_j|`

If the denominator is zero, the implementation must define a deterministic fallback rather than divide by zero.

Recommended fallback:

```text
q_n = 0 for all n
```

and therefore aggregate controls are zero for that period.

---

# 37. Step 5 — Aggregate Portfolio Controls

The aggregate portfolio is:

`b_{m,t+1} = Σ_n q_{n,t+1} h^n_{m,t}`

In matrix form:

`b_{t+1} = H_t q_{t+1}`

This portfolio becomes the position used for the next trading period.

---

# 38. Complete Daily Loop

The daily implementation should conceptually execute:

```text
FOR each trading day t:

    1. Read OHLCV through t.

    2. Update technical indicators.

    3. Generate raw signal for every expert and asset.

    4. Convert hold signals into persistent positions.

    5. Transform signals into expert portfolio controls.

    6. Compute expert wealth using the previously held controls
       and the newly realized price relatives.

    7. Update expert mixture weights from accumulated wealth.

    8. Normalize expert mixture to zero cost and unit leverage.

    9. Aggregate expert controls.

   10. Store aggregate controls for t+1.

   11. Calculate/record realized aggregate return.

   12. Apply transaction costs corresponding to portfolio changes.

END
```

The exact implementation should preserve the distinction between:

```text
information date
decision date
holding period
realization date
```

---

# 39. Expert Population Construction

The expert generator must enumerate all permitted combinations.

For each:

```text
strategy ω
cluster c
n1
n2
```

create a unique expert ID.

Recommended ID:

```text
expert_id =
strategy + cluster + n1 + n2
```

Examples:

```text
MOM_ALL_4
MOM_ALL_8
MACD_ALL_4_20
RSI_ALL_14
BOLL_ALL_20
```

The exact lookback grid must be stored in configuration rather than hard-coded.

---

# 40. Lookback Grid

The paper's general framework allows multiple short and long lookbacks.

Because the user's dataset is only one year, the lookback grid must be kept computationally feasible.

Recommended daily grid for an implementation experiment:

```text
short_lookbacks = [4, 8, 12, 16, 20]
long_lookbacks  = [20, 40, 60, 90]
```

For two-parameter rules retain only:

```text
n1 < n2
```

IMPORTANT:

This grid is an **implementation adaptation**, not a claim that these exact values reproduce every parameter used in the original paper.

If the exact supplementary-material grid is available, replace this section with that grid and label it "paper-exact configuration".

---

# 41. Minimum History

Because some rules require long lookbacks and volatility estimation requires a 90-day history, the strategy should not be activated before enough historical observations exist.

For strict daily implementation:

```text
minimum_history =
max(
    maximum_indicator_lookback,
    90-day volatility lookback,
    liquidity lookback
)
```

If using 90-day liquidity and volatility:

```text
minimum_history >= 90 trading days
```

If the largest technical lookback exceeds 90, use that value instead.

No padding with future data is permitted.

---

# 42. Initial Wealth

Initialize:

```text
aggregate_wealth = 1.0
expert_wealth[n] = 1.0 for all n
cumulative_PL = 0.0
```

The aggregate portfolio is zero before the first valid decision.

---

# 43. Transaction Costs

The paper emphasizes that transaction costs are critical.

Daily costs include:

- spread;
- price impact;
- slippage;
- direct fees;
- borrowing/regulatory/short-related costs.

The paper's daily transaction-cost model uses a square-root price-impact formulation:

`TC = M × Spread + σ × sqrt(n / ADV)`

where:

- `M` = number of signal changes;
- `Spread` = assumed spread cost;
- `σ` = volatility;
- `n` = number of shares traded;
- `ADV` = average daily volume.

For the daily implementation the paper assumes:

- ADV based on previous 90 days;
- traded shares equal to 1 basis point of ADV for each stock per day;
- spread assumption of 1 bp/day;
- direct cost assumption of 4 bp/day.

The paper reports an average daily transaction cost of roughly 17.75 bp under its resulting daily assumptions.

These figures are **paper assumptions for its JSE setting** and should not automatically be interpreted as realistic costs for another market.

---

# 44. Transaction-Cost Implementation for Dummy Data

If the dummy dataset lacks:

- bid/ask spread;
- market impact data;
- shares outstanding;
- realistic order size;

do not fabricate market microstructure.

Use a clearly declared simplified cost model.

Recommended sensitivity grid:

```text
0 bps
5 bps
10 bps
20 bps
50 bps
```

Apply costs to **turnover**, not simply to every day.

For portfolio weights:

`turnover_t = Σ_i |w_{i,t} - w_{i,t-1}|`

Then:

`net_return_t = gross_return_t - cost_rate × turnover_t`

This simplified model is an adaptation and should be reported separately from the paper's original square-root implementation.

---

# 45. Gross vs Net Experiments

Every run should produce at least two versions:

## Gross

No transaction costs.

## Net

Transaction costs applied.

Optional sensitivity:

```text
net_5bp
net_10bp
net_20bp
net_50bp
```

The key question is whether the performance survives increasing friction.

---

# 46. Benchmark

The paper compares the online strategy against an offline BCRP benchmark.

The BCRP is a hindsight constant rebalanced portfolio selected using historical data.

For the paper:

- 5000 random CRP strategies are generated;
- the strategy with maximum terminal wealth is selected;
- the original BCRP comparison is long-only.

For a one-year constrained replication:

### Required benchmark

At minimum:

```text
Buy-and-hold / equal-weight benchmark
```

### Paper-style benchmark

Implement random CRP search:

1. sample random non-negative weights;
2. normalize weights to sum to one;
3. evaluate terminal wealth over the designated benchmark sample;
4. retain the highest terminal wealth.

**Important:** this is a hindsight benchmark. It must never be used as an out-of-sample benchmark without explicitly labeling it as such.

---

# 47. Benchmark Leakage Warning

The BCRP is a hindsight benchmark.

Therefore:

```text
BCRP ≠ investable out-of-sample strategy
```

It is a comparison object, not evidence of implementable predictive power.

The pipeline should report it separately from the online portfolio.

---

# 48. Performance Metrics

For every aggregate strategy and major benchmark calculate:

## Cumulative return

`CR = S_T / S_0 - 1`

## Annualized return

For `N` trading observations:

`AR = (S_T/S_0)^(252/N) - 1`

For a one-year dataset this will be close to the cumulative return but should still be calculated from the actual observation count.

## Volatility

Using daily returns:

`σ_ann = std(r_daily) × sqrt(252)`

## Sharpe ratio

With zero risk-free return:

`Sharpe = mean(r_daily) / std(r_daily) × sqrt(252)`

If a risk-free series is supplied, subtract it first.

## Maximum drawdown

Calculate:

`Drawdown_t = S_t / max_{u<=t}(S_u) - 1`

Then:

`MDD = min(Drawdown_t)`

## Calmar ratio

`Calmar = annualized_return / |MDD|`

## Hit rate

`hit_rate = number_of_positive_periods / number_of_nonzero_return_periods`

## Profit factor

`PF = gross_profit / gross_loss`

where gross loss is positive in magnitude.

## Turnover

`Turnover_t = Σ_i |w_{i,t} - w_{i,t-1}|`

Report:

- average daily turnover;
- total turnover;
- turnover distribution.

---

# 49. Expert-Level Diagnostics

Do not only evaluate the aggregate portfolio.

For every expert calculate:

```text
expert_id
strategy
cluster
n1
n2
terminal_wealth
cumulative_return
annualized_return
volatility
sharpe
max_drawdown
turnover
mean_daily_PL
std_daily_PL
```

Also calculate:

- expert rank by terminal wealth;
- mean rank by strategy family;
- rank dispersion;
- best/worst expert;
- median expert;
- mean expert;
- proportion of experts profitable.

The paper itself examines expert wealth and groups experts by their underlying strategy.

---

# 50. Strategy-Family Aggregation

For each strategy family:

```text
EMA X-over
Ichimoku
MACD
Moving Average X-over
ACC
BOLL
Fast Stochastic
MARSI
MOM
Online Anti-Z-BCRP
Online Z-Anticor
Online Z-BCRP
PROC
RSI
SAR
Slow Stochastic
Williams %R
```

calculate:

- number of experts;
- mean terminal wealth;
- median terminal wealth;
- standard deviation of terminal wealth;
- minimum terminal wealth;
- maximum terminal wealth;
- mean expert rank;
- mean daily P/L;
- standard deviation of daily P/L.

These statistics reproduce the paper's strategy-family analysis.

---

# 51. Expert Wealth Matrix

Construct:

```text
date × expert_id
```

containing:

`S^h_{n,t}`

This matrix is important because it supports:

- expert-performance visualization;
- expert clustering;
- VAE dimensionality reduction;
- PBO analysis;
- strategy-family comparisons.

Do not discard individual expert wealth after calculating the aggregate portfolio.

---

# 52. Population Dynamics

The research pipeline should analyze:

```text
expert wealth trajectories
expert ranks
expert mixture weights
strategy-family wealth
asset/cluster exposure
```

The central research object is therefore not just:

```text
portfolio equity curve
```

but:

```text
population of expert trajectories
        ↓
online wealth competition
        ↓
adaptive mixture
        ↓
aggregate portfolio
```

---

# 53. VAE / Unsupervised Expert Analysis

The paper applies a Variational Autoencoder (VAE) to the time series of expert wealth and visualizes the resulting latent space.

This is a **secondary analysis**, not required to generate the trading portfolio.

Input:

```text
expert wealth matrix
```

Prefer normalized wealth trajectories or returns so that the VAE is not dominated by absolute scale.

Suggested preprocessing:

```text
log_wealth = log(S^h)
```

or:

```text
expert_returns
```

The exact preprocessing must be documented.

Output:

```text
latent_dimension_1
latent_dimension_2
```

Then produce two plots:

1. color/label by asset cluster;
2. color/label by technical strategy family.

The paper uses this to examine whether expert wealth dynamics cluster more strongly according to asset selection or strategy type.

---

# 54. Statistical-Arbitrage Definition

A strategy is considered a statistical arbitrage candidate if its discounted cumulative trading profits `ν(t)` satisfy:

1. `ν(0) = 0`;
2. `lim E[ν(t)] > 0`;
3. `lim P[ν(t) < 0] = 0`;
4. `lim Var[Δν(t) | Δν(t) < 0] = 0`.

These are asymptotic theoretical conditions.

The finite-sample test is therefore an empirical approximation rather than a direct proof of the asymptotic definition.

---

# 55. Incremental Profit Model

The paper models discounted incremental profits as:

`Δν_i = μ i^θ + σ i^λ z_i`

under the unconstrained-mean model.

Two innovation cases are considered:

### UM

`z_i` are independent standard normal variables.

### UMC

`z_i = ε_i + φ ε_{i-1}`

with normal innovations and MA(1)-type correlation.

Corresponding constrained-mean versions set:

`θ = 0`

giving:

`Δν_i = μ + σ i^λ z_i`

The models are used to derive hypotheses for statistical arbitrage.

---

# 56. Statistical-Arbitrage Hypotheses

For the UM model, the paper states that statistical arbitrage requires the joint satisfaction of:

### H1

`μ > 0`

### H2

`-λ > 0` OR `θ - λ > 0`

### H3

`θ - λ + 1/2 > 0`

### H4

`θ + 1 > 0`

The no-statistical-arbitrage null is the union/complement of these restrictions.

Therefore the implementation must test the relevant sub-hypotheses rather than simply testing whether average return is positive.

---

# 57. Min-t Test

The paper uses the Min-t statistic based on the relevant parameter restrictions.

Compute parameter estimates using maximum likelihood and obtain t-statistics for:

```text
μ
-λ
θ-λ
θ-λ+0.5
θ+1
```

The exact statistic is the minimum of the relevant one-sided test statistics after accounting for the alternative/null formulation.

The empirical conclusion is:

```text
reject no-statistical-arbitrage
```

only if the observed Min-t exceeds the simulated critical value.

---

# 58. Monte Carlo Critical Value

For the CM model the paper:

1. simulates 5000 profit processes;
2. uses:
   `μ = 0`
   `λ = 0`
   `σ² = 0.01`;
3. estimates model parameters for each simulated process;
4. calculates the corresponding Min-t statistic;
5. uses the `1-α` quantile of simulated Min-t statistics as the critical value.

Recommended:

```text
alpha = 0.05
N_sim = 5000
```

The random seed must be fixed for reproducibility.

---

# 59. P-Value

Calculate the empirical probability that the simulated Min-t statistic exceeds the observed Min-t statistic.

Report:

```text
observed_Min_t
critical_value
empirical_p_value
alpha
reject_null
```

Do not report a p-value without also reporting the simulated critical value and test configuration.

---

# 60. Probability of Loss

The paper computes the probability of loss after `n` periods for:

```text
n = 1,...,T
```

For each horizon:

1. estimate the relevant model parameters using the first `n` observations;
2. substitute the estimates into the probability-of-loss formula;
3. record the resulting probability.

Plot:

```text
horizon n
    vs
probability of loss
```

The paper discusses convergence toward zero, with 5% used as a practical reference in the literature.

For the one-year dataset, report this as an empirical finite-sample diagnostic.

---

# 61. PBO — Probability of Back-Test Overfitting

The paper uses the CSCV methodology of Bailey et al. (2016).

The objective is to determine how likely it is that strong apparent performance results from selecting a strategy based on historical performance.

For this implementation, construct a matrix:

```text
rows = candidate/trial strategy runs
columns = chronological performance observations
```

Do not randomly shuffle individual daily observations.

The time structure must remain intact.

---

# 62. CSCV Procedure

Let:

```text
N = number of trial strategies/runs
T = number of performance observations
```

Split the history into `S` chronological/equal-length subsets.

Generate combinatorial train/test partitions.

For every partition:

1. calculate in-sample performance for every candidate;
2. select the best candidate using the chosen performance metric;
3. evaluate that selected candidate on the corresponding out-of-sample subset;
4. record whether the selected strategy underperforms the median or loses relative rank out of sample;
5. compute the logit transform used by the PBO methodology.

The final PBO is the proportion of partitions in which the in-sample winner fails according to the PBO criterion.

---

# 63. Important PBO Interpretation

A high PBO means:

> the strategy-selection process is frequently producing an in-sample winner that does not remain strong out of sample.

A low PBO means:

> the selection procedure appears more stable across the CSCV partitions.

PBO is not a probability that the strategy will lose money tomorrow.

---

# 64. One-Year Data Limitation

A one-year daily dataset is only approximately 252 observations.

This is much shorter than the six-year daily dataset used by the paper.

Consequences:

- technical indicators have fewer independent observations;
- long lookbacks consume a large fraction of the sample;
- statistical-arbitrage tests have low power;
- PBO/CSCV partitions become less stable;
- expert wealth rankings can be highly path-dependent;
- transaction-cost estimates are less reliable;
- annualized metrics can be noisy.

Therefore the project must be described as:

> **a constrained methodological replication**

rather than a full empirical reproduction of the original results.

---

# 65. Recommended One-Year Experimental Split

If the goal is to evaluate out-of-sample behavior:

```text
Days 1–126   : development / initial learning history
Days 127–189 : validation / monitoring
Days 190–252 : final out-of-sample evaluation
```

However, because the paper's online learner is sequential, the preferred implementation is a **walk-forward simulation** rather than a conventional random train/test split.

No random train/test split is permitted.

---

# 66. Walk-Forward Principle

At every date:

```text
history available at t
        ↓
calculate indicators
        ↓
generate expert signals
        ↓
generate portfolio
        ↓
hold through next period
        ↓
observe next-period return
        ↓
update wealth
        ↓
repeat
```

The learner never receives future prices before they occur.

---

# 67. Strict Alignment Invariants

The implementation should enforce these assertions:

```text
feature_date <= decision_date
signal_date <= decision_date
portfolio_date = decision_date + 1 period
realized_return_date > decision_date
```

For each trade:

```text
signal uses only information available by signal timestamp
execution does not use future information
cost uses current/previous portfolio weights only
return uses subsequent realized price
```

If any row violates this, the backtest must fail validation.

---

# 68. Missing Data Rules

Never forward-fill prices across actual missing trading observations unless justified.

Recommended:

- missing OHLCV → asset unavailable for that date;
- insufficient indicator history → signal unavailable;
- insufficient volatility history → weight unavailable;
- denominator equal to zero → indicator unavailable;
- invalid price relative → no portfolio return for that asset.

The pipeline must not replace missing historical values with future values.

---

# 69. Corporate Actions

If real equities are used:

- split adjustments;
- dividends;
- ticker changes;
- delistings;
- survivorship bias

must be considered.

For a dummy dataset, document whether prices are:

```text
raw
split-adjusted
total-return adjusted
```

The implementation should not silently mix price conventions.

---

# 70. Required Output Tables

## Table A — Dataset summary

```text
asset
start_date
end_date
n_observations
mean_volume
missing_rate
```

## Table B — Expert summary

```text
expert_id
strategy
cluster
n1
n2
terminal_wealth
return
sharpe
max_drawdown
turnover
rank
```

## Table C — Strategy-family summary

```text
strategy
n_experts
mean_terminal_wealth
median_terminal_wealth
std_terminal_wealth
min_terminal_wealth
max_terminal_wealth
mean_rank
mean_PL
std_PL
```

## Table D — Aggregate performance

```text
strategy
gross_return
net_return
annualized_return
volatility
sharpe
max_drawdown
calmar
turnover
```

## Table E — Cost sensitivity

```text
cost_bps
return
sharpe
max_drawdown
turnover
```

## Table F — Statistical arbitrage

```text
model
observed_Min_t
critical_value
p_value
reject_null
```

## Table G — PBO

```text
number_of_trials
number_of_partitions
selection_metric
PBO
```

---

# 71. Required Plots

At minimum:

## Plot 1 — Aggregate equity curve

```text
online aggregate wealth
vs
benchmark wealth
```

## Plot 2 — Aggregate cumulative P/L

```text
PL_t
```

## Plot 3 — Expert wealth heatmap

Rows:

```text
expert
```

Columns:

```text
date
```

Values:

```text
normalized expert wealth
```

## Plot 4 — Strategy-family wealth

One line per strategy family.

## Plot 5 — Expert rank distribution

Show how rankings vary across experts.

## Plot 6 — Mixture weights

Show the evolution of the largest expert/strategy contributions.

## Plot 7 — Drawdown

Aggregate strategy drawdown.

## Plot 8 — Cost sensitivity

Performance versus transaction-cost assumption.

## Plot 9 — Probability of loss

Horizon versus estimated probability of loss.

## Plot 10 — VAE latent space

Color once by asset cluster and once by strategy family.

## Plot 11 — PBO / CSCV distribution

Show the distribution of out-of-sample performance or the PBO statistic according to the implemented CSCV procedure.

---

# 72. Baselines

The online strategy must be compared against simple baselines.

Required:

### Baseline 1 — Buy and hold

Use the same asset universe.

### Baseline 2 — Equal-weight portfolio

Rebalance according to the specified schedule.

### Baseline 3 — Individual technical rules

Evaluate selected individual strategy families without meta-learning.

### Baseline 4 — Zero portfolio / cash

Useful as a control for zero-cost strategies.

### Baseline 5 — BCRP

Use only as the paper-style hindsight benchmark.

---

# 73. Ablation Experiments

The implementation should support the following experiments.

## Ablation A — no meta-learning

Average or equally weight experts.

Compare with the adaptive learner.

Hypothesis:

```text
adaptive aggregation > static aggregation
```

if the online learning mechanism adds value.

---

## Ablation B — one technical family

Run the learner using only one rule family.

Compare:

```text
MOM only
RSI only
MACD only
BOLL only
etc.
```

against the full expert population.

---

## Ablation C — no transaction costs

Compare:

```text
gross
net
```

---

## Ablation D — cost sensitivity

Compare:

```text
0 / 5 / 10 / 20 / 50 bps
```

---

## Ablation E — volatility weighting

Compare:

```text
paper volatility loading
vs
equal-weight active positions
```

This is an extension, not part of the strict paper replication.

---

## Ablation F — lookback sensitivity

Run multiple predefined lookback grids.

Do not select the grid after inspecting final test performance.

---

# 74. Statistical Comparison

Where feasible, compare daily strategy returns against the benchmark using:

- mean return difference;
- paired return differences;
- bootstrap confidence intervals;
- Sharpe ratio comparison;
- maximum drawdown;
- turnover-adjusted performance.

Because financial returns are non-normal and serially dependent, avoid relying exclusively on a simple independent-samples t-test.

For a one-year sample, confidence intervals should be interpreted cautiously.

---

# 75. Reproducibility Requirements

Every experiment must record:

```text
dataset identifier
date range
asset universe
lookback grid
strategy families
cluster definition
volatility window
liquidity window
cost assumptions
risk-free assumption
initial wealth
random seed
Monte Carlo simulations
CSCV configuration
software/library versions
```

The full configuration should be serialized with each run.

Recommended:

```text
config.yaml
```

or equivalent structured configuration.

---

# 76. Deterministic Randomness

Any stochastic procedure must have an explicit seed.

Examples:

- BCRP random portfolio generation;
- Monte Carlo Min-t simulations;
- bootstrap procedures;
- VAE initialization;
- CSCV randomization, if used.

The seed must be stored in experiment metadata.

---

# 77. Numerical Stability

The implementation must protect against:

```text
division by zero
NaN
Inf
zero volatility
zero ADV
zero Bollinger range
zero RSI denominator
zero portfolio normalization denominator
```

Any fallback must be deterministic and documented.

---

# 78. Expert Eligibility

An expert is active only if all required historical inputs are available.

Examples:

```text
MOM(90)
```

requires sufficient price history.

```text
MACD(n1,n2,n3)
```

requires sufficient EMA history.

```text
volatility loading
```

requires sufficient volatility history.

An expert with insufficient information should produce:

```text
signal = 0
control = 0
```

rather than use future information or a fabricated indicator.

---

# 79. Core Data Structures

Recommended internal objects:

```text
MarketData
    date
    asset_id
    OHLCV

IndicatorStore
    indicator_name
    asset_id
    date
    parameters
    value

SignalStore
    expert_id
    asset_id
    date
    raw_signal
    persistent_signal

ExpertControls
    expert_id
    date
    asset_id
    weight

ExpertWealth
    expert_id
    date
    wealth
    period_return

MixtureWeights
    date
    expert_id
    raw_q
    normalized_q

AggregatePortfolio
    date
    asset_id
    weight

PortfolioPerformance
    date
    gross_return
    transaction_cost
    net_return
    wealth
    drawdown
```

---

# 80. Pseudocode — Full Implementation

```python
initialize market_data
validate_schema(market_data)
sort_by_asset_and_date(market_data)

initialize expert_definitions
initialize expert_wealth = 1 for every expert
initialize aggregate_wealth = 1
initialize previous_aggregate_weights = zero

for t in valid_trading_dates:

    history = market_data up to and including t

    update_liquidity(history)
    update_technical_indicators(history)

    for expert in expert_definitions:

        for asset in expert.asset_universe:

            raw_signal = expert.rule(
                asset_history_through_t,
                expert.parameters
            )

            persistent_signal = apply_hold_persistence(
                raw_signal,
                previous_signal[expert, asset]
            )

        controls = transform_signals_to_controls(
            persistent_signals,
            volatility_history_through_t
        )

        store controls

    # Return realized at t using controls established previously
    price_relatives_t = close_t / close_t_minus_1

    aggregate_return_t = portfolio_return(
        previous_aggregate_weights,
        price_relatives_t
    )

    expert_returns_t = {}
    for expert:
        expert_returns_t[expert] = portfolio_return(
            previous_expert_controls[expert],
            price_relatives_t
        )

        expert_wealth[expert] *= (
            1 + expert_returns_t[expert]
        )

    aggregate_wealth *= (
        1 + aggregate_return_t
    )

    raw_q = expert_wealth.copy()

    centered_q = raw_q - mean(raw_q)

    denominator = sum(abs(centered_q))

    if denominator > 0:
        q = centered_q / denominator
    else:
        q = zero_vector()

    next_aggregate_weights = (
        sum_over_experts(
            q[expert] * expert_controls[expert]
        )
    )

    transaction_cost_t = calculate_cost(
        previous_aggregate_weights,
        next_aggregate_weights,
        market_data,
        cost_config
    )

    net_return_t = aggregate_return_t - transaction_cost_t

    store all diagnostics

    previous_aggregate_weights = next_aggregate_weights
    previous_expert_controls = current_expert_controls
    previous_signals = current_signals
```

The implementation may optimize this computation, but the temporal semantics must remain unchanged.

---

# 81. Important Temporal Correction for Implementation

The pseudocode above should be interpreted carefully:

- today's close is available after today's market close;
- today's signal can therefore determine tomorrow's position;
- today's realized return cannot be earned using a signal that depends on today's close.

The canonical alignment is:

```text
OHLCV[t]
   ↓
indicator[t]
   ↓
signal[t]
   ↓
control[t+1]
   ↓
return[t+1]
```

This is the most important anti-lookahead invariant in the entire implementation.

---

# 82. What Counts as Successful Replication?

A successful implementation is **not** defined as reproducing the paper's exact terminal wealth.

Because the user's dataset differs materially from the paper's:

```text
paper:
JSE
15 stocks
~6 years daily
specific market microstructure
specific risk-free asset
specific cost assumptions

current experiment:
dummy dataset
~1 year daily
possibly different number of assets
possibly no risk-free series
possibly no spread/order-book data
```

Therefore success means:

1. the algorithmic mechanics match the paper;
2. the temporal alignment is correct;
3. the expert population is generated correctly;
4. signals are transformed correctly;
5. online mixture learning is implemented correctly;
6. costs are explicitly modeled;
7. benchmarks are explicit;
8. diagnostics are reproducible;
9. deviations from the paper are documented.

---

# 83. Primary Evaluation Question

The final experiment should answer:

> **Does the Murphy–Gebbie online aggregation of technical-trading experts generate a robust aggregate portfolio under a constrained one-year daily-data environment, and does any apparent performance survive transaction costs and out-of-sample evaluation?**

---

# 84. Secondary Evaluation Questions

1. Which technical strategy families produce the strongest experts?
2. How dispersed are expert outcomes?
3. Does adaptive aggregation outperform static expert weighting?
4. Does the aggregate strategy outperform buy-and-hold?
5. Does the aggregate strategy outperform equal weighting?
6. How much performance disappears after transaction costs?
7. How does turnover relate to performance?
8. Does the aggregate strategy exhibit statistical-arbitrage evidence?
9. Does that evidence survive transaction costs?
10. What is the estimated PBO?
11. Are expert wealth dynamics clustered by strategy or asset universe?
12. How sensitive are conclusions to the lookback grid?
13. How sensitive are conclusions to cost assumptions?
14. Does the strategy remain viable when the final portion of the sample is kept strictly out of sample?

---

# 85. Decision Rules

The implementation should classify the final outcome using the following logic.

## Strong evidence

Only if:

- positive net cumulative return;
- competitive risk-adjusted return;
- acceptable drawdown;
- performance persists out of sample;
- performance survives reasonable transaction costs;
- statistical evidence is not obviously destroyed by multiple testing;
- PBO is not excessively high.

## Weak evidence

If:

- gross returns are positive;
- but net performance is weak;
- or out-of-sample performance deteriorates;
- or PBO is high.

## No evidence

If:

- gross performance is not positive;
- or net performance is negative;
- or the strategy does not outperform simple baselines;
- or apparent performance disappears under realistic costs.

Do not redefine the hypothesis after seeing results.

---

# 86. Strict vs Extended Implementation

The codebase should expose two modes.

## `STRICT_PAPER`

Implements only mechanisms directly supported by the paper:

- daily OHLCV;
- technical expert population;
- signal persistence;
- volatility loading;
- zero-cost/unit-leverage controls;
- online wealth-weighted mixture;
- daily transaction-cost framework;
- BCRP benchmark;
- statistical-arbitrage test;
- PBO;
- expert population analysis.

## `CONSTRAINED_DATA`

Adapts the implementation to one-year dummy data:

- reduced lookback grid;
- available asset universe;
- simplified risk-free assumption if required;
- simplified transaction costs if market microstructure is unavailable;
- walk-forward evaluation.

Every result must contain:

```text
implementation_mode
```

so the two modes cannot be confused.

---

# 87. What Must NOT Be Done

The pipeline must not:

- randomly shuffle time-series observations;
- use future closes to calculate today's signal;
- use full-sample liquidity to select assets;
- use full-sample volatility for historical weights;
- optimize lookbacks on the final test period;
- select the best expert using the final out-of-sample period;
- report only gross returns;
- omit turnover;
- omit transaction costs;
- silently replace missing data with future values;
- silently change the paper's signal inequalities;
- treat `hold` as automatic liquidation;
- claim a one-year test reproduces the paper's six-year JSE result;
- use BCRP hindsight performance as an investable benchmark;
- call a positive backtest proof of statistical arbitrage.

---

# 88. Minimum Viable Implementation

If engineering time is limited, implement in this order:

### Phase 1

- data validation;
- price relatives;
- SMA/EMA;
- 5–8 technical rules;
- signals;
- hold persistence;
- expert controls;
- expert wealth.

### Phase 2

- all technical rules;
- all expert parameter combinations;
- online mixture;
- aggregate portfolio;
- benchmark.

### Phase 3

- transaction costs;
- turnover;
- cost sensitivity;
- expert population diagnostics.

### Phase 4

- statistical-arbitrage Min-t;
- probability of loss;
- PBO/CSCV.

### Phase 5

- VAE expert population analysis.

A result from Phase 1–3 is a partial implementation. It should not be described as a complete replication until Phases 4–5 are also addressed or explicitly excluded.

---

# 89. Final Research Deliverables

The pipeline should produce:

```text
results/
    dataset_summary.csv
    expert_summary.csv
    strategy_summary.csv
    aggregate_performance.csv
    benchmark_performance.csv
    cost_sensitivity.csv
    statistical_arbitrage.csv
    pbo_results.csv

    expert_wealth.parquet
    expert_controls.parquet
    aggregate_controls.parquet
    daily_returns.parquet

    figures/
        aggregate_equity.png
        aggregate_drawdown.png
        expert_wealth_heatmap.png
        strategy_family_wealth.png
        expert_rank_distribution.png
        mixture_weights.png
        cost_sensitivity.png
        probability_of_loss.png
        vae_latent_space.png
        pbo_distribution.png

    config/
        experiment_config.yaml
        random_seeds.yaml
```

---

# 90. Final Hypothesis Statement

The complete research hypothesis is:

> **A diverse population of parameterized technical-trading experts, when transformed into self-financing zero-cost portfolios and aggregated through an online wealth-weighted learning algorithm, can produce an adaptive portfolio whose performance is competitive with standard benchmarks. However, the economic significance of any apparent performance should be judged only after accounting for transaction costs, out-of-sample behavior, statistical-arbitrage tests, and the probability of back-test overfitting.**

For the one-year daily dummy dataset, this should be treated as a **methodological and constrained replication** of Murphy & Gebbie (2019), not as a reproduction of their original empirical findings.

---

# 91. Source Traceability

Primary source used for this implementation specification:

**Murphy, N. J. & Gebbie, T. (2019). Learning the dynamics of technical trading strategies. arXiv:1903.02228v3.**

The supplied paper describes:

- the expert-population approach;
- technical indicators and trading rules;
- signal-to-control transformation;
- zero-cost/unit-leverage constraints;
- online wealth-weighted expert aggregation;
- daily transaction-cost modeling;
- BCRP benchmarking;
- statistical-arbitrage testing;
- probability-of-loss analysis;
- CSCV/PBO;
- VAE analysis of expert wealth.

The paper reports daily experiments using the 15 most liquid JSE Top 40 stocks over a six-year period. This differs from the present one-year dummy-data environment and is why the constrained-data adaptations above are explicitly separated from paper-exact methodology.

---

# 92. Implementation Acceptance Tests

Before trusting any result, the pipeline must pass the following tests.

## Test 1 — No lookahead

For a given date `t`, modifying all observations after `t` must not change:

```text
indicator[t]
signal[t]
expert_control[t]
```

## Test 2 — Signal shift

A signal generated at `t` must affect returns beginning at `t+1`, not the return ending at `t`.

## Test 3 — Hold persistence

```text
previous = +1
current raw = 0
=> current effective position = +1
```

and:

```text
previous = -1
current raw = 0
=> current effective position = -1
```

## Test 4 — Zero-cost constraint

For every active expert:

```text
abs(sum(weights)) < tolerance
```

## Test 5 — Unit leverage

For every active expert:

```text
abs(sum(abs(weights)) - 1) < tolerance
```

## Test 6 — New signal overrides hold

```text
previous = -1
current raw = +1
=> effective = +1
```

## Test 7 — Expert wealth accounting

Manually verify at least one expert:

```text
wealth_t
=
wealth_{t-1}
×
[1 + Σ weight_{t-1} × return_t]
```

## Test 8 — Aggregate accounting

Verify:

```text
aggregate_weights
=
Σ expert_mixture × expert_weights
```

## Test 9 — Cost accounting

Verify:

```text
net_return
=
gross_return - transaction_cost
```

## Test 10 — Reproducibility

Running the same configuration twice with the same seed must produce identical outputs up to numerical precision.

---

# 93. Researcher Checklist

Before publishing or presenting results:

- [ ] Dataset schema documented
- [ ] Date range documented
- [ ] Asset universe documented
- [ ] Liquidity selection documented
- [ ] Lookback grid documented
- [ ] All strategy rules documented
- [ ] Hold persistence implemented
- [ ] No-lookahead test passed
- [ ] Expert controls validated
- [ ] Zero-cost constraint validated
- [ ] Unit-leverage constraint validated
- [ ] Expert wealth stored
- [ ] Aggregate wealth stored
- [ ] Gross results calculated
- [ ] Net results calculated
- [ ] Turnover calculated
- [ ] Cost sensitivity calculated
- [ ] Buy-and-hold benchmark calculated
- [ ] Equal-weight benchmark calculated
- [ ] BCRP benchmark calculated/documented as hindsight
- [ ] Statistical-arbitrage test implemented or explicitly excluded
- [ ] Probability-of-loss analysis implemented or explicitly excluded
- [ ] PBO/CSCV implemented or explicitly excluded
- [ ] VAE analysis implemented or explicitly excluded
- [ ] Random seeds recorded
- [ ] Configuration stored
- [ ] One-year limitation disclosed
- [ ] No claims made that exceed the available evidence

---

# 94. Bottom Line

The implementation should be thought of as:

```text
                    DAILY OHLCV
                        │
                        ▼
              ┌──────────────────┐
              │ Technical Rules  │
              │ + Parameters     │
              └────────┬─────────┘
                       │
                       ▼
               Expert Signals
             {-1, 0, +1}
                       │
                       ▼
              Hold Persistence
                       │
                       ▼
              Volatility Loading
                       │
                       ▼
             Expert Portfolios
                       │
                       ▼
                Expert Wealth
                       │
                       ▼
          Wealth-Based Online Mixture
                       │
                       ▼
              Aggregate Portfolio
                       │
              ┌────────┴────────┐
              ▼                 ▼
        Gross Returns      Trading Costs
              │                 │
              └────────┬────────┘
                       ▼
                Net Performance
                       │
        ┌──────────────┼───────────────┐
        ▼              ▼               ▼
    Benchmark      Statistical       PBO/CSCV
                   Arbitrage
        │              │               │
        └──────────────┼───────────────┘
                       ▼
              Research Conclusion
```

The central object is therefore **not a single trading signal**. It is the **dynamic population of experts and the online mechanism that converts their historical wealth into the next portfolio**.
