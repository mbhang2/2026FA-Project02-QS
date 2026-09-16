# Audit agent demos

Use these for slides and OpenCode walkthroughs. Run from repo root.

## CLI

```bash
python3 -m audit run --id 01-lookahead-cheat --brief
python3 -m audit run --id 01-realistic --brief
python3 -m audit run --id nvda-1m-realistic --brief
python3 -m audit ledger --brief
```

## OpenCode

```text
/audit kill
/audit pause
/audit nvda-1m-realistic
```

## Expected outcomes

| id | Strategy | Data | action | Teaching point |
|---|---|---|---|---|
| `01-lookahead-cheat` | LookaheadCheat | SYNTH | **kill** | Sharpe 20 still dies on leak |
| `01-realistic` | TsMom | SYNTH | **pause** | Legal formula, weak PSR |
| `nvda-1m-realistic` | BuyHold | NVDA 1m | **pause** | Vendor path works; not the paper |

## Do not conflate

- `01-realistic` = Moskowitz TSMOM on **synthetic daily**
- `nvda-1m-realistic` = **buy-hold** on **vendor** 1-minute — not TSMOM
