# 2026FA-Project02-2-QS — AutoResearch for Quantitative Finance

A Fall 2026 MSFE practicum with QuantStrat. This repository is an **automated
research loop for trading strategies that does not fool itself.** A language
model proposes strategies against a pre-registered research question, an
isolated harness backtests each one, and a gate decides whether anything was
found — **correcting for every trial it took to find it**, crashed ones included.

**The deliverable is the loop and its gate, not a profitable strategy.** Systems
that keep a change whenever one number improves are false-discovery machines once
the number is a Sharpe ratio. This one counts every look and pays for it.

## How it works

1. **A question.** Either a committed hypothesis file, or a one-line statement
   that the writer turns into one (`--question`).
2. **A campaign.** The proposer (an LLM) writes N strategies against the
   question, each edited from the best so far. It sees scores, never the gate.
3. **A trial.** Each strategy runs in its own sandboxed process on the
   training split; every run, crashed or not, is one row in the trial ledger.
4. **The gate.** Each candidate gets its own null; the campaign's best is
   corrected for N (`max_of_n`). Four LLM auditors can veto.
5. **The next question.** In a family, a refiner diagnoses what went wrong,
   retrieves papers for it (or uses its own judgement), and writes question k+1.
6. **The stop.** Validation runs once per campaign at the end of a session, the
   L2 gate corrects across campaigns, and the holdout is opened at most once, ever.

The rules for all of this are pre-registered in
[`docs/design/pre-registration.md`](docs/design/pre-registration.md).

## Run it

```bash
pip install -r requirements.lock          # mypy included: the agent's static check needs it
pytest -q                                 # offline; no credentials needed

# one campaign, from a statement
python scripts/nq_campaign.py --stage register --name nq-me-01 \
       --question "I want a trend reversal strategy that looks into CVD"
python scripts/nq_campaign.py --stage run    --name nq-me-01
python scripts/nq_campaign.py --stage nulls  --name nq-me-01
python scripts/nq_campaign.py --stage report --name nq-me-01

# a whole session on synthetic data, end to end, without spending a look
python scripts/rehearse.py --campaigns 3 --n 4 \
       --seed-question "I want a trend reversal strategy that looks into CVD"
```

Model access goes through a LiteLLM gateway (`LITELLM_BASE_URL`,
`LITELLM_API_KEY`); see [`docs/setup/MODEL-ACCESS.md`](docs/setup/MODEL-ACCESS.md)
for which models serve themselves today.

## Where to read next

**[`docs/README.md`](docs/README.md) is the map**: which document covers which
part, and where to look first when something breaks.
[`docs/FILES.md`](docs/FILES.md) says what every file does, and
[`docs/RUN-SEQUENCE.md`](docs/RUN-SEQUENCE.md) which files a run calls, in order.

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
