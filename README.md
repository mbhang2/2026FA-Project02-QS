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
| `CLAUDE.md` | repo conventions and the hard constraints. Read first. |
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

## OpenCode onboarding guide for UIUC practicum interns

Welcome to the team! To get you started with our LLM infrastructure, we need to configure your local OpenCode client.

We have set up a public-facing reverse proxy to handle requests. This means **you do not need to be connected to the internal network or VPN** to use OpenCode.

Please follow the steps below to configure your local environment.

### Step 1: Set Environment Variables

You will receive your individual API key from your admin. Once you have it, add the following lines to your shell profile (e.g., `~/.zshrc` or `~/.bashrc`):

```bash
export LITELLM_API_KEY="<YOUR_ASSIGNED_API_KEY>"
export LITELLM_BASE_URL="https://llm-proxy.invalid/v1"
```

After adding the lines, remember to reload your shell profile:
```bash
source ~/.zshrc  # or source ~/.bashrc
```

If you aren't using linux/Mac, you may need to sort out how to set environment variables for your OS. Bryan or Brian can help if you get stuck.

### Step 2: Configure OpenCode

Next, you need to configure OpenCode to use the reverse proxy and your API key.

Create or edit your OpenCode configuration file at `~/.config/opencode/opencode.json` (or `opencode.jsonc` if it already exists).  Other operating systems may have different paths for the configuration file, so please adjust accordingly.

ensure it includes the following content:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": [
    "opencode-plugin-litellm@latest"
  ],
  "provider": {
    "litellm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LiteLLM",
      "options": {
        "baseURL": "{env:LITELLM_BASE_URL}",
        "apiKey": "{env:LITELLM_API_KEY}",
        "chunkTimeout": 300000,
        "timeout": 1860000
      }
    }
  },
  "subagent_depth": 2
}
```

### Step 3: Verify the Setup

To confirm that everything is set up correctly and your client can communicate with the proxy, run the following command in your terminal:

```bash
opencode models
```

If the command succeeds and lists the available models without error, you are good to go!

Some models will be blocked at the gateway for cost reasons, including Anthropic's Opus/Sonnet (Haiku is available, and quite good), OpenAI's Sol/Terra (Luna is available, and highly recommended for deterministic tasks). Some of the Kimi models, etc.  We encourage you to use "smart" frontier or near frontier models as architects or designers, but I think you'll find that with use of our existing multi-agent patterns, smaller models produce equal or better results than a single large model trying to one-shot a complex task.  The goal is to use the right tool for the job, not just the biggest model.  We'll see this as we develop an agentic auto-research flow as well.  teams of small models orchestrated correctly can have higher accuracy, but at the expense of time and complexity vs speed and cost.  The right balance is what we are trying to find, and we will be doing this in a research context, so you will have the opportunity to help us find that balance.

If you run into any issues, double-check your API key and verify that your environment variables are correctly loaded by running `echo $LITELLM_BASE_URL` in your terminal.

Happy coding!


