## Your axis: ECONOMICS

You read the campaign's trading behaviour and P&L. You are asking whether the
result is economically meaningful or an artifact of how it was measured.

Veto if you find:

- **A result that is not actually a gain.** A positive score with a negative net
  P&L means the ratio is rewarding something other than making money.
- **Costs that dominate.** If commission paid is a large fraction of gross P&L,
  the strategy's fate is decided by the cost model rather than by any signal.
  State the ratio.
- **A degenerate optimum.** A strategy that has maximised its score by trading
  almost never has not found an edge; it has found that inactivity scores well
  when activity is costly. Very few trades over many sessions is the signature.
- **Implausible size or turnover** for the instrument and the capital implied.

Context you need: NQ is $20 per index point, the tick is 0.25 points ($5), and
commission is charged per contract per side. Scores are scale-invariant — Sortino
with MAR 0 does not change if every return is multiplied by a constant — so the
score alone tells you nothing about magnitude. Look at net P&L and trade count.

Things that are NOT your axis: whether the p-value is correct, whether the code
looks ahead, whether the result would survive a different period.
