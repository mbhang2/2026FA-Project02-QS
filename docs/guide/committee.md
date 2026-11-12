# The committee

Four LLM auditors read one finished campaign and any one may veto it:
**leakage, statistics, economics, robustness**. It runs in the `report` stage.
It can never approve: "no auditor vetoed" is not a pass, and the record says so.

[Documentation map](../README.md) · deep design: [design/architecture.md](../design/architecture.md) §8c, [design/tier-contract.md](../design/tier-contract.md)

## Files

| File | Role |
|---|---|
| `audit/committee/review.py` | Runs the four auditors in parallel and builds the verdict (`AUDIT_MODEL`) |
| `audit/committee/llm.py` | Its own model client. `audit/` imports nothing from `src/` |
| `audit/committee/prompts/*.md` | One prompt per auditor, plus `_shared.md` |
| `audit/tier3.py` | The deterministic checks that run before the committee |
| `scripts/nq_campaign.py` (`stage_report`, `committee_evidence`) | Hands each auditor its evidence; retries transient failures |
| `scripts/committee_retry.py` | Re-runs seats that never ran, beside the original record |
| `runs/campaigns/<id>/committee-verdict.json` | The verdict |

## How it works

- **Isolation is the mechanism.** Each auditor gets its own call and its own
  slice of evidence, and never sees another's verdict, so none rubber-stamps the
  others. Only leakage sees the source; leakage does not see the gate's verdict.
- **Economics checks the link nobody else does**: does the code implement the
  pre-registered question? It sees the hypothesis, the source and the costs.
- **Verdicts per auditor**: `VETO`, `CANNOT_TELL`, or `PASS` (nothing to veto on
  its own axis). The campaign's verdict is `VETO` if any auditor vetoed.
- **None of them knows how many campaigns came before**, so the look count is
  never a reason to pass or fail.
- **Its model** defaults to `mimo-v2.6-pro`; set `--audit-model` at registration
  (plan key `audit_model`). It is frozen in `campaign.json` so the committee
  cannot be chosen after seeing the trials. Ideally it is not the proposer's model.
- **Its objections feed the next question.** In a family, the refiner's diagnosis
  reads them (see [hypotheses.md](hypotheses.md)).

## When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `committee-verdict.json` says `"verdict": "ERROR"` | The committee did not run; recorded, explicitly not a pass | The `error` field; usually a model substitution, so set `--audit-model` |
| An auditor `CANNOT_TELL` citing missing evidence | Its slice lacked something it needed | Its `reasons`; `scripts/committee_retry.py` re-runs seats |
| Every campaign vetoed by robustness | Profit concentrated in a few sessions | Often a real finding; the refiner gets it as evidence |
| A refinement had no auditor objections to work from | The committee errored on that campaign | As the first row |
