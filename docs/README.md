# Documentation map

**Start here.** Find the part of the system you care about, read its guide, and
follow the guide's links into the deep documents only if you need the reasoning.

- **New to the repo?** Read the [top-level README](../README.md), then
  [RUN-SEQUENCE.md](RUN-SEQUENCE.md).
- **Looking for a file?** [FILES.md](FILES.md) lists every file and what it does
  (generated from the files themselves).
- **Something broke?** Use the symptom table at the bottom.

## Parts of the system

| Part | What it covers | Read first | Then, for depth |
|---|---|---|---|
| **Proposer** | The LLM that writes strategies: prompt, static check, repairs, probe, fidelity checks | [guide/proposer.md](guide/proposer.md) | [design/loop.md](design/loop.md), [agent/strategy-api.md](agent/strategy-api.md), [design/context-api.md](design/context-api.md) |
| **Harness** | `run.py`, the engine, the isolated child process, the trial ledger | [guide/harness.md](guide/harness.md) | [design/architecture.md](design/architecture.md) §2-5c, 7, [design/simulator-interface.md](design/simulator-interface.md) |
| **Scoring and the gate** | The metric, per-candidate nulls, the correction | [guide/scoring.md](guide/scoring.md) | [design/metric.md](design/metric.md), [design/pre-registration.md](design/pre-registration.md) §3-5 |
| **Campaign** | One campaign's stages and the files it writes | [guide/campaign.md](guide/campaign.md) | [setup/RUNNING-A-CAMPAIGN.md](setup/RUNNING-A-CAMPAIGN.md) |
| **Hypotheses** | Question files, writing from a statement, refinement, paper retrieval | [guide/hypotheses.md](guide/hypotheses.md) | [design/paper-retrieval.md](design/paper-retrieval.md), pre-registration Amendments 8, 11, 14, 15 |
| **Sessions** | Families, validation at the stop, L2, the holdout, rehearsals | [guide/sessions.md](guide/sessions.md) | pre-registration §4-8, Amendments 10, 12, 13 |
| **Committee** | The four LLM auditors that may veto a campaign | [guide/committee.md](guide/committee.md) | [design/architecture.md](design/architecture.md) §8c, [design/tier-contract.md](design/tier-contract.md) |
| **Models and gateway** | Which models answer, substitution, timeouts, caching, model options | [guide/models.md](guide/models.md) | [setup/MODEL-ACCESS.md](setup/MODEL-ACCESS.md), [setup/LOCAL_PROXY.md](setup/LOCAL_PROXY.md) |
| **Data and environment** | Datasets, splits, R2, the Python environment, the Linux runner | [guide/data-and-environment.md](guide/data-and-environment.md) | [agent/data.md](agent/data.md), [datasets/nq-es-qqq.md](datasets/nq-es-qqq.md), [setup/R2_ACCESS.md](setup/R2_ACCESS.md), [setup/LINUX_RUNNER.md](setup/LINUX_RUNNER.md) |

**The rules** behind every part are in
[design/pre-registration.md](design/pre-registration.md): the protocol and its
numbered amendments. It is long because it records why; the guides say what.

### Strategy families (design notes, for review)

[design/ast-strategies.md](design/ast-strategies.md) ·
[design/ml-strategies.md](design/ml-strategies.md) ·
[design/rl-strategies.md](design/rl-strategies.md) ·
[design/market-making.md](design/market-making.md)

### History and evidence

[reviews/](reviews/) holds the script behind every number quoted in these
documents, one folder per task. [plans/](plans/) holds build plans, and
[knowledge/](knowledge/) the store of settled findings the proposer is shown.

## Something broke: where to look

| Symptom | Likely cause | Look in |
|---|---|---|
| Every trial crashes with `static_check_unrepaired`, last message "mypy is not installed" | The Python running campaigns has no mypy | [guide/data-and-environment.md](guide/data-and-environment.md) |
| `ModelSubstituted: asked 'X', served 'gemini-…'` | The gateway is answering with a different model | [guide/models.md](guide/models.md) |
| `HTTP 502` after almost exactly 300 s | The Apache proxy in front of the gateway timed out a long reply | [guide/models.md](guide/models.md) |
| Proposals keep failing the probe; many `probe_repair` calls | The proposal crashes or does nothing on synthetic data | [guide/proposer.md](guide/proposer.md) |
| Trials marked "off question" | The strategy does not do what the question fixes | [guide/proposer.md](guide/proposer.md), [guide/hypotheses.md](guide/hypotheses.md) |
| A trial ran for hours, then `strategy_exceeded_resources` | CPU limit hit; wall clock allowed far longer | [guide/harness.md](guide/harness.md) |
| `rejected: exposed in 0 of N sessions` | The strategy never traded; below the exposure floor | [guide/scoring.md](guide/scoring.md) |
| "every null sample is EMPTY" | No scored candidate, or fills not merged from workers | [guide/scoring.md](guide/scoring.md), [guide/campaign.md](guide/campaign.md) |
| `--stage run` refuses: hypothesis digest changed | The question file was edited after registration | [guide/campaign.md](guide/campaign.md) |
| `register needs exactly one of --hypothesis … or --question` | No question given; there is no default | [guide/campaign.md](guide/campaign.md) |
| `NOT registered -- the review seat did not clear it` | Writing from a statement: the reviewer could not tell, or did not run | [guide/hypotheses.md](guide/hypotheses.md) |
| Refinement `REFUSED: … seat(s) never read this question` | The reviewers' model is unavailable | [guide/hypotheses.md](guide/hypotheses.md), [guide/models.md](guide/models.md) |
| `REFUSED. A validation artefact is readable` | Refining after validation ran; only allowed before the stop | [guide/sessions.md](guide/sessions.md) |
| `no validation profile 'X-validation'` | The training profile has no validation pair in `config/run.yaml` | [guide/sessions.md](guide/sessions.md) |
| Committee verdict `ERROR` | The auditors' model failed; recorded, not a pass | [guide/committee.md](guide/committee.md) |
| A campaign takes 10+ hours | Long replies, 502 hangs, runaway backtests, the generation barrier | [guide/campaign.md](guide/campaign.md) |
| The test suite refuses to start: "a campaign is running" | It shares `src/strategies/current.py` with the run | [guide/harness.md](guide/harness.md) |
