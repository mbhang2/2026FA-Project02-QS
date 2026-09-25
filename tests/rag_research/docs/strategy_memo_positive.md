# Quantitative Strategy Memo: Macroeconomic Inflation Dynamics

## Empirical Evidence (KB-1 Literature)
Annual inflation has slowed in recent months, with year-over-year CPI inflation matching 2.7% through December 2025 driven by moderating rent of housing readings.

[evidence] "Over the twelve months ending in December 2025 ... although energy inflation has accelerated very recently."

Core CPI was 2.6% over the twelve months through December 2025, which is 0.3 percentage points below the 12-month rate recorded through June.

Year-over-year rent of housing inflation has moderated substantially, slowing by 0.8 percentage points compared to the pace recorded through June.

## Algorithmic Candidate Specification
```json
{
  "strategy_name": "MacroInflationMomentum",
  "asset_class": "fixed_income",
  "universe": ["TLT", "IEF", "SHY"],
  "timeframe": "1d",
  "parameters": {
    "cpi_target": 2.7,
    "core_cpi": 2.6,
    "moderation_pace": 0.8
  },
  "consensus": "AGREE"
}
```
