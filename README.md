# 2026FA-Project02-2-QS — AutoResearch for Quantitative Finance

A Fall 2026 MSFE practicum with QuantStrat. We are building an **automated research
loop**: an agent proposes a change to a trading strategy, the harness backtests it, and a
gate keeps or discards it.

**The deliverable is the loop and its keep/discard gate, not a profitable strategy.**
Every system in the reference bibliography accepts a change when a single scalar improves,
and none corrects for the hundreds of trials it took to find that scalar. In LLM
pretraining that is survivable; ported to a Sharpe ratio it is a false-discovery machine.
The contribution is the correction that pattern lacks, over a trial ledger that counts
crashed and abandoned runs as trials.

The harness is dataset-agnostic — datasets, fill models and metrics are interfaces, and
the NQ/ES/QQQ futures data is the first adapter rather than an assumption.

## State, as of Task 34

The loop runs end to end on the pre-registered training split. A stub agent proposes
candidates, each is scored in its own sandboxed process, the campaign's trials go through
a multiple-testing correction, and the gate decides. Two numbers say where it stands, and
both were measured rather than argued:

| | |
|---|---|
| Family-wise error on a known-null campaign | **0.060**, CI [0.041, 0.088], at q = 0.05 and the operating R = 150 |
| Detection of a planted signal | **0.90** at N = 200, prevalence 0.05, strength 0.30 |

Not done: validation and holdout are still closed, the proposing agent is a stub rather
than an LLM, and the committee tier does not exist.

## Where to start reading

| | |
|---|---|
| `docs/plans/l1-build-plan.md` | the harness, and the build order it is being built in. Nothing dataset-specific. |
| `docs/design/architecture.md` | the map: every module, and why it exists. A test asserts it stays complete. |
| `docs/design/metric.md` | the metric, the null, the correction, and every calibration number, layered by task. |
| `docs/agent/strategy-api.md` | how to write a strategy. Its worked examples run in CI. |
| `docs/datasets/nq-es-qqq.md` | the first adapter: coverage, invariants, corrections, falsified hypotheses. |
| `docs/setup/R2_ACCESS.md` | data access for teammates. `LINUX_RUNNER.md` next to it is how to stand up a scoring host. |
| `docs/reviews/` | the script behind every number quoted anywhere above. |

## Running it

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                      # the whole suite; offline, no credentials needed
python run.py --help           # the harness entry point
python scripts/campaign.py --help
```

Live tests re-read the R2 bucket and skip when `R2_*` is absent. Scored campaigns should
run on Linux — macOS isolation is best-effort and cannot bind a memory ceiling; every run
record carries its isolation level so a best-effort row can be discounted.

---

## Team

Michael Bhang, Nalin Agrawal, Daksh Dagariya and Gowtam Polamarasetti, with mentors Brian G. Peterson and Bryan Rodriguez, who also contributed the agent patterns and the RAG test suite.

Teammates' independent work lives on its own branches: `Gowtam-L1`, `Gowtam/HypInt`, `nalin/l1-loop`, `user/daksh/MVP-prompt` and `feature/rag-autoresearch`.

## About this public release

This repository was published from the team's private working repository. Licensed market data, bulky generated run output and a private service endpoint were removed from its history, which gives every commit a new id. [`docs/PUBLIC_RELEASE.md`](docs/PUBLIC_RELEASE.md) lists exactly what changed, and `docs/release/commit-map.tsv` maps the commit ids the trial ledgers record to the ids in this repository.

## Acknowledgments

Developed as a UIUC MSFE practicum project (Fall 2026) sponsored by QuantStrat.

## License

MIT. See [LICENSE](LICENSE).
