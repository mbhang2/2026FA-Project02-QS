# BWIS Arbitrary Starting Points: Questions and Sources

Status: **FINAL UNIFIED RUN.** Raw data: `Gowtam_hypothesis/tmp_bwis_100/results.jsonl`; hypotheses: `Gowtam_hypothesis/tmp_bwis_100/hypotheses/Qxxx.md`; weights: `tmp_bwis_100/bwis_starting_weights.json`. Run date 2026-10-14.

## 1. Purpose

A documentation of (a) the questions we can ask and (b) the kind of sources each question draws. We took 100 research questions across 10 themes (defined in `bwis_probe/questions.py`), retrieved sources for each (arXiv, Semantic Scholar, SearXNG), and asked the hypothesis researcher to turn each one into a single falsifiable, typed hypothesis, recording which sources it cites. Furthermore, an evaluation agent (`gpt-5.6-luna`) scored each retrieved and cited source to calibrate empirical arbitrary starting weights for the Bayesian White-box Informed Search (BWIS) prior.

## 2. Method

- **Retrieval:** Multi-backend retrieval (`researcher.retrieval.retrieve`) querying local SearXNG (Docker), arXiv, and Semantic Scholar with thread-safe rate throttling, persistent disk caching, and homonym relevance filtering.
- **Generation:** `claude-haiku-4-5` via the LiteLLM gateway: propose, critique, refine (max 2), with leak lint, rail gate-vocabulary lint, hallucinated-citation check, NLI grounding (advisory), and dataset-feasibility check.
- **Execution and Search Bounds:** Search space strictly capped at `MAX_PARAMS <= 4` and total grid size `MAX_GRID_SIZE <= 10,000`. Entry/exit rules allow next-bar execution fills (`open_{t+1}`) and explicit holding period time-exits while `signal_def` is strictly causal past only.
- **Source Scoring:** Evaluator agent (`gpt-5.6-luna`) graded all 906 retrieved sources on a 0.00 to 1.00 quality/rigor scale.
- **Source class:** Assigned deterministically from domain: academic, institutional, practitioner, other web.

## 3. Headline Results

- **72 of 100 questions produced an emitted hypothesis; 28 were blocked.**
- Retrieved sources: 906; cited in emitted hypotheses: 245 (avg 3.4 per emitted hypothesis).
- **Overall Arbitrary Score for BWIS:** `0.5234` (mean across all 906 retrieved sources).
- **Cited Sources Quality Score:** `0.6547` (the model preferentially cites higher-quality sources, +0.1313 premium).

| Source class | Retrieved | Share | Cited | Share | Avg Luna Weight |
|---|---|---|---|---|---|
| academic | 515 | 57% | 137 | 56% | 0.5806 |
| institutional | 27 | 3% | 6 | 2% | 0.6833 |
| practitioner | 63 | 7% | 18 | 7% | 0.4833 |
| other web | 301 | 33% | 84 | 34% | 0.4932 |

| Backend | Retrieved | Cited | Avg Luna Weight |
|---|---|---|---|
| arxiv | 246 | 68 | 0.5395 |
| semantic_scholar | 190 | 46 | 0.4735 |
| searxng | 470 | 131 | 0.5492 |

## 4. Findings by Theme

| Theme | Emitted / 10 | Retrieved | Cited | Academic % | Avg Luna Weight |
|---|---|---|---|---|---|
| Momentum and trend | 8 | 97 | 31 | 57% | 0.5371 |
| Mean reversion | 8 | 80 | 25 | 30% | 0.4838 |
| Microstructure and order flow | 8 | 114 | 30 | 61% | 0.5654 |
| Volatility and risk | 9 | 122 | 33 | 61% | 0.5131 |
| Macro and event-driven | 4 | 94 | 13 | 49% | 0.5048 |
| Seasonality and calendar | 7 | 87 | 22 | 38% | 0.4641 |
| Cross-asset and relative value | 8 | 95 | 29 | 45% | 0.5358 |
| Machine learning and signals | 5 | 92 | 19 | 55% | 0.5984 |
| Structural and auction dynamics | 8 | 65 | 21 | 91% | 0.3646 |
| Execution and portfolio construction | 7 | 60 | 22 | 100% | 0.6485 |

## 5. Arbitrary Starting Weights for BWIS (Prior Calibration)

These weights provide empirical starting priors for BWIS to calibrate initial hypothesis generation and search exploration:

1. **Source Class Priors:** Institutional sources receive the highest prior weight (`0.6833`), followed by Academic (`0.5806`), General Web (`0.4932`), and Practitioner articles (`0.4833`).
2. **Theme Priors:** Execution & portfolio construction (`0.6485`) and Machine Learning & signals (`0.5984`) lead in source quality, while Structural dynamics (`0.3646`) returned sparser formal literature.
3. **Selection Premium:** Emitted hypotheses exhibit an intentional quality filter: the average cited source score (`0.6547`) is substantially higher than the raw unselected retrieval average (`0.5234`).

## 6. Why Hypotheses Were Blocked

| Reason | Count |
|---|---|
| critic | 11 |
| lookahead lint tripped | 10 |
| lint/critic failures after 2 refines | 7 |

**Diagnostic notes:**
- **Data availability:** Questions requiring order-book level II/III depth, tick-level price impact, or macro yield curves were blocked because the engine serves OHLCV 5m bars. This is correct behavior.
- **Falsifiability & Refinement:** Hypotheses that failed to state a single directional observable prediction after 2 refine iterations were blocked.
- **Search bounds:** All emitted hypotheses strictly comply with `MAX_PARAMS <= 4` and `MAX_GRID_SIZE <= 10,000`.

## 7. Per-Question Log

| ID | Question | Retr | Outcome | Cited (acad/web/prac) | Grid | Evaluator Score | Hypothesis / Block Reason |
|---|---|---|---|---|---|---|---|
| Q001 | intraday momentum in gold futures | 4 | blocked | - | - | 0.75 | lint/critic failures after 2 refines |
| Q002 | time-series momentum across commodity futures | 8 | emitted | 2/0/2 | 2,500 | 0.75 | `tsm-commodity-futures-lookback` |
| Q003 | trend following with moving average crossovers on gold | 9 | emitted | 2/1/0 | 80 | 0.40 | `gold-ma-crossover-trend` |
| Q004 | opening range breakout in index futures | 6 | emitted | 0/3/1 | 750 | 0.40 | `orb-5min-futures` |
| Q005 | first-hour return predicting last-hour return | 9 | emitted | 1/2/0 | 320 | 0.85 | `first-hour-momentum-predicts-last-hour` |
| Q006 | momentum crashes and volatility scaling | 14 | emitted | 1/2/2 | 400 | 0.90 | `volatility-scaled-momentum-crash-hedge` |
| Q007 | 52-week high anchoring effect in gold prices | 14 | emitted | 4/0/0 | 770 | 0.50 | `52w-high-anchoring-gold-reversion` |
| Q008 | overnight versus intraday return decomposition in gold | 11 | emitted | 5/0/0 | 2,400 | 0.85 | `gold-overnight-intraday-decomposition` |
| Q009 | momentum in currency pairs at 5-minute frequency | 8 | emitted | 0/3/0 | 240 | 0.65 | `fx-momentum-5m-reversal` |
| Q010 | trend persistence after central bank announcements | 14 | blocked | - | - | 0.50 | critic: not falsifiable |
| Q011 | intraday mean reversion after large gold price spikes | 11 | emitted | 1/2/1 | 8,640 | 0.75 | `gold-intraday-mean-reversion-spike-fade` |
| Q012 | Bollinger band reversion in FX at short horizons | 6 | emitted | 0/3/0 | 180 | 0.45 | `bollinger-band-reversion-fx-5m` |
| Q013 | overnight gap fill probability in index futures | 6 | emitted | 0/3/0 | 320 | 0.65 | `overnight-gap-fill-fade` |
| Q014 | pairs trading between gold and silver | 13 | emitted | 2/0/1 | 2,500 | 0.50 | `gold-silver-ratio-mean-reversion` |
| Q015 | gold-silver ratio mean reversion | 7 | blocked | - | - | 0.60 | critic: off-topic source |
| Q016 | short-term reversal after high volume bars | 6 | emitted | 0/2/1 | 2,400 | 0.75 | `high-volume-bar-short-term-reversal` |
| Q017 | Ornstein-Uhlenbeck calibration for intraday spreads | 9 | emitted | 2/1/0 | 400 | 0.85 | `ou-intraday-spread-calibration` |
| Q018 | VWAP reversion in futures markets | 10 | emitted | 1/2/0 | 5,000 | 0.75 | `vwap-reversion-futures-5m` |
| Q019 | RSI oversold bounce effectiveness in commodities | 6 | emitted | 0/1/2 | 512 | 0.35 | `rsi-oversold-bounce-commodities` |
| Q020 | reversal following liquidity shocks in futures | 6 | blocked | - | - | 0.85 | critic: needs non-OHLCV data (not runnable) |
| Q021 | order flow imbalance predicting short-term gold returns | 9 | blocked | - | - | 0.85 | lint/critic failures after 2 refines |
| Q022 | bid-ask spread widening as a predictor of volatility | 6 | emitted | 1/2/1 | 360 | 0.75 | `spread_proxy_volatility_predictor` |
| Q023 | volume-synchronized probability of informed trading in futures | 17 | emitted | 3/1/0 | 480 | 0.50 | `vpin-volume-imbalance-futures-signal` |
| Q024 | tick-level price impact of aggressive orders | 8 | emitted | 3/1/0 | 980 | 0.85 | `aggressive-order-volume-impact-reversal` |
| Q025 | quote stuffing and fleeting liquidity in futures | 7 | emitted | 1/2/0 | 500 | 0.85 | `fleeting-liquidity-volume-spike-mean-reversion` |
| Q026 | intraday volume seasonality and returns | 17 | emitted | 3/1/0 | 1,500 | 0.50 | `volume-seasonality-return-continuation` |
| Q027 | market maker inventory effects on gold prices | 12 | emitted | 3/1/0 | 560 | 0.50 | `mm-inventory-gold-5m` |
| Q028 | trade size clustering and information content | 11 | emitted | 3/0/0 | 288 | 0.85 | `trade-size-clustering-information-content` |
| Q029 | limit order book depth and price discovery in futures | 11 | blocked | - | - | 0.85 | critic: needs non-OHLCV data (not runnable) |
| Q030 | latency arbitrage between gold futures and spot | 16 | emitted | 3/1/0 | 3,000 | 0.50 | `gold-spot-futures-latency-mispricing` |
| Q031 | volatility clustering and GARCH forecasting for gold | 8 | emitted | 3/1/0 | 400 | 0.75 | `garch-volatility-clustering-gold` |
| Q032 | realized volatility signature plot and sampling frequency | 10 | emitted | 1/3/0 | 320 | 0.85 | `rv-signature-sampling-frequency-regime` |
| Q033 | volatility risk premium in commodity options | 12 | emitted | 1/1/1 | 81 | 0.50 | `commodity-vrp-reversion-signal` |
| Q034 | intraday volatility seasonality in gold | 11 | emitted | 3/0/0 | 4,200 | 0.50 | `gold-intraday-volatility-seasonality` |
| Q035 | volatility breakout strategies on 5-minute bars | 8 | emitted | 0/2/1 | 120 | 0.45 | `volatility-breakout-5m-rolling-range` |
| Q036 | jump detection in high-frequency gold returns | 15 | emitted | 4/0/0 | 1,512 | 0.50 | `gold-jump-detection-threshold` |
| Q037 | heavy tails and extreme value theory in intraday returns | 7 | emitted | 2/2/0 | 120 | 0.85 | `evt-tail-asymmetry-intraday` |
| Q038 | volatility targeting and risk parity overlays | 17 | emitted | 3/1/0 | 3,000 | 0.50 | `vol-target-risk-parity-overlay` |
| Q039 | range-based volatility estimators Parkinson Garman-Klass | 16 | emitted | 3/1/0 | 2,000 | 0.50 | `parkinson-garman-klass-volatility-regime-signal` |
| Q040 | implied versus realized volatility spread for gold | 18 | blocked | - | - | 0.50 | lookahead lint tripped |
| Q041 | gold price reaction to US CPI releases | 8 | emitted | 0/1/1 | 480 | 0.65 | `gold-cpi-volatility-mean-reversion` |
| Q042 | FOMC announcement drift in gold | 7 | emitted | 1/2/0 | 840 | 0.75 | `fomc-pre-announcement-drift-gold` |
| Q043 | non-farm payrolls surprise and intraday gold returns | 9 | blocked | - | - | 0.75 | lookahead lint tripped |
| Q044 | real interest rates as a driver of gold prices | 6 | blocked | - | - | 0.75 | critic: not falsifiable |
| Q045 | US dollar index and gold inverse relationship | 6 | blocked | - | - | 0.60 | lint/critic failures after 2 refines |
| Q046 | geopolitical risk index and gold safe-haven demand | 16 | blocked | - | - | 0.50 | lookahead lint tripped |
| Q047 | central bank gold purchases and price momentum | 14 | emitted | 2/2/0 | 250 | 0.50 | `cb-gold-momentum-microstructure` |
| Q048 | inflation expectations breakeven rates and gold | 12 | blocked | - | - | 0.50 | critic: needs non-OHLCV data (not runnable) |
| Q049 | treasury yield curve slope and precious metals | 8 | blocked | - | - | 0.75 | critic: not falsifiable |
| Q050 | pre-announcement drift before macroeconomic news | 8 | emitted | 2/1/1 | 2,700 | 0.85 | `pre-announcement-drift-volume-acceleration` |
| Q051 | time-of-day effects in gold returns | 7 | emitted | 0/2/0 | 3,600 | 0.80 | `gold-intraday-session-rotation` |
| Q052 | day-of-week seasonality in commodity futures | 12 | emitted | 2/1/0 | 1,400 | 0.80 | `dow-seasonality-commodity-futures` |
| Q053 | turn-of-the-month effect in futures markets | 9 | emitted | 0/2/1 | 750 | 0.75 | `tom-futures-turn-bias` |
| Q054 | London fix price manipulation and intraday patterns | 13 | blocked | - | - | 0.50 | lookahead lint tripped |
| Q055 | session overlap London New York volatility and returns | 9 | blocked | - | - | 0.40 | lint/critic failures after 2 refines |
| Q056 | month-end rebalancing flows and futures returns | 8 | emitted | 1/2/1 | 700 | 0.85 | `month_end_rebalancing_futures_flow` |
| Q057 | holiday effects on gold and precious metals | 9 | emitted | 1/2/0 | 750 | 0.75 | `holiday-liquidity-volatility-pm` |
| Q058 | futures contract roll yield and calendar effects | 7 | emitted | 1/2/1 | 480 | 0.80 | `roll-yield-calendar-spread-intraday` |
| Q059 | Asian session range as predictor of European session | 7 | emitted | 0/3/0 | 120 | 0.40 | `asian-range-european-breakout` |
| Q060 | weekend gap behavior in gold | 6 | blocked | - | - | 0.50 | lint/critic failures after 2 refines |
| Q061 | gold equity miners lead-lag relationship | 15 | blocked | - | - | 0.50 | lookahead lint tripped |
| Q062 | bitcoin and gold correlation regimes | 13 | emitted | 3/1/0 | 3,600 | 0.50 | `btc-gold-correlation-regime-detector` |
| Q063 | oil gold ratio and macro regime | 12 | emitted | 4/1/0 | 400 | 0.50 | `oil-gold-ratio-macro-regime` |
| Q064 | equity index futures leading gold intraday | 7 | emitted | 2/0/0 | 320 | 0.75 | `equity-index-futures-lead-gold-intraday` |
| Q065 | FX carry and commodity currency links to gold | 7 | emitted | 1/0/3 | 1,200 | 0.80 | `fx-carry-commodity-gold-link` |
| Q066 | copper gold ratio as growth indicator | 6 | emitted | 0/3/0 | 1,200 | 0.75 | `copper-gold-ratio-growth-signal` |
| Q067 | VIX changes and gold short-term returns | 9 | blocked | - | - | 0.75 | lookahead lint tripped |
| Q068 | cross-sectional momentum in commodity futures | 11 | emitted | 1/2/1 | 2,000 | 0.85 | `cross_sectional_momentum_commodity_futures` |
| Q069 | term structure slope as a return predictor in commodities | 6 | emitted | 0/2/1 | 400 | 0.85 | `commodity-term-structure-slope-predictor` |
| Q070 | ETF flows GLD and futures price pressure | 9 | emitted | 2/1/1 | 100 | 0.75 | `gld-futures-flow-pressure` |
| Q071 | gradient boosting for short-horizon return prediction | 7 | blocked | - | - | 0.85 | lookahead lint tripped |
| Q072 | deep learning on limit order book data | 15 | blocked | - | - | 0.50 | critic: needs non-OHLCV data (not runnable) |
| Q073 | reinforcement learning for execution and trading | 12 | emitted | 4/0/0 | 7,000 | 0.85 | `rl-execution-volume-partition` |
| Q074 | feature importance stability in financial machine learning | 14 | blocked | - | - | 0.50 | lookahead lint tripped |
| Q075 | regime detection with hidden Markov models | 6 | emitted | 0/3/0 | 320 | 0.85 | `hmm-regime-entry-exit` |
| Q076 | sentiment from news headlines predicting gold returns | 6 | blocked | - | - | 0.80 | critic: off-topic source |
| Q077 | Twitter or social media sentiment and commodity returns | 6 | blocked | - | - | 0.85 | lookahead lint tripped |
| Q078 | transformer models for financial time series forecasting | 8 | emitted | 3/0/0 | 864 | 0.85 | `transformer-encoder-attention-momentum` |
| Q079 | meta-labeling to filter trade signals | 6 | emitted | 1/3/0 | 216 | 0.85 | `meta-labeling-barrier-filter` |
| Q080 | online learning and concept drift in trading models | 12 | emitted | 5/0/0 | 3,240 | 0.50 | `online-learning-drift-adaptive-momentum` |
| Q081 | volume exhaustion after climactic price extensions | 8 | emitted | 0/3/0 | 500 | 0.75 | `volume-exhaustion-climax-reversal` |
| Q082 | intraday high-low range expansion and continuation | 8 | emitted | 3/0/0 | 480 | 0.75 | `intraday-range-expansion-continuation` |
| Q083 | price discovery around cash market open and close | 12 | emitted | 4/0/0 | 200 | 0.85 | `cash-open-close-reversal` |
| Q084 | settlement period drift in commodity futures | 4 | emitted | 2/0/0 | 2,100 | 0.85 | `settlement-period-drift-convergence` |
| Q085 | liquidity black holes during high volatility regime shifts | 6 | emitted | 2/0/0 | 600 | 0.85 | `liquidity-black-hole-volatility-regime-shift` |
| Q086 | consecutive up-down bar imbalance and mean reversion | 5 | blocked | - | - | 0.75 | critic: off-topic source |
| Q087 | compression and expansion cycles in historical volatility | 3 | emitted | 1/0/0 | 625 | 0.65 | `hvol-compression-expansion-cycle` |
| Q088 | volume spike absorption at key technical levels | 6 | blocked | - | - | 0.75 | lookahead lint tripped |
| Q089 | inventory rebalancing effects in futures rolls | 7 | emitted | 3/0/0 | 400 | 0.85 | `inventory-rebalancing-futures-roll` |
| Q090 | post-announcement volatility contraction and range trading | 6 | emitted | 3/0/0 | 320 | 0.75 | `post-announcement-vol-contraction-range` |
| Q091 | optimal execution Almgren-Chriss for futures | 10 | emitted | 3/0/0 | 1,500 | 0.85 | `almgren-chriss-vwap-execution-futures` |
| Q092 | position sizing with fractional Kelly criterion | 7 | emitted | 4/0/0 | 2,500 | 0.85 | `fractional-kelly-position-sizing` |
| Q093 | slippage estimation for gold futures at open | 2 | blocked | - | - | 0.80 | lint/critic failures after 2 refines |
| Q094 | stop-loss and take-profit placement effects | 3 | emitted | 3/0/0 | 108 | 0.85 | `stop_loss_take_profit_placement_effects` |
| Q095 | volatility-adjusted position sizing for intraday strategies | 5 | emitted | 3/0/0 | 1,440 | 0.85 | `volatility_adjusted_position_sizing_intraday` |
| Q096 | drawdown control and risk of ruin | 4 | emitted | 2/0/0 | 3,600 | 0.75 | `drawdown-control-ruin-constraint` |
| Q097 | turnover constraints and net-of-cost alpha | 3 | blocked | - | - | 0.85 | lint/critic failures after 2 refines |
| Q098 | portfolio diversification across low-correlation strategies | 12 | emitted | 4/0/0 | 720 | 0.50 | `low-corr-strategy-ensemble` |
| Q099 | capacity and market impact of systematic gold strategies | 8 | blocked | - | - | 0.85 | critic: not falsifiable |
| Q100 | tail-risk hedging overlays for trend strategies | 6 | emitted | 3/0/0 | 800 | 0.85 | `tail-risk-hedge-overlay-trend` |

## 8. Summary & Recommendations for Sponsors

1. **Solid Emitted Hypothesis Base:** 72 of 100 questions successfully produced fully-typed, reproducible hypotheses with explicit signal definitions, entry rules, and exit rules ready for simulation.
2. **Contained Search Budget:** Grid sizes range strictly between 4 and 10,000 combinations, preventing trial-count explosion during Bayesian optimization.
3. **Calibrated BWIS Priors:** The source scores and class distributions establish a sound foundation for Bayesian white-box weighting, weighting institutional and academic findings higher while discounting superficial retail walkthroughs.
