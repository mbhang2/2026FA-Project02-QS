# Running a campaign

Anyone on the team can run one. The mechanics are five commands. **The part that
needs care is not the running — it is the accounting**, and §3 is the bit to read
before you start rather than after.

## 1. What you need that is not in the repo

| | |
|---|---|
| `secrets/r2.env` | the R2 credentials. Gitignored, so ask Michael. Read-only; five variables, `R2_ENDPOINT` `R2_BUCKET` `R2_ACCOUNT_ID` `R2_ACCESS_KEY_ID` `R2_SECRET_ACCESS_KEY` |
| `LITELLM_BASE_URL`, `LITELLM_API_KEY` | the model gateway |
| ~1.1 GiB of disk | `data/cache/` fills from R2 on the first NQ run. Gitignored, and it is a cache — losing it costs a download, not data |

`.envrc` and `requirements.lock` are tracked. `direnv allow`, then
`pip install -r requirements.lock`.

**Never commit a credential.** `secrets/` is ignored, and `git add .` is one typo
away from permanent history. Stage by name.

## 2. The five commands

```
python scripts/nq_campaign.py --stage register --name nq-<you>-01 \
       --hypothesis config/hypotheses/<your-question>.md
python scripts/nq_campaign.py --stage run      --name nq-<you>-01   # ~3-4 hours
python scripts/nq_campaign.py --stage nulls    --name nq-<you>-01   # ~2 minutes
python scripts/nq_campaign.py --stage validate --name nq-<you>-01   # ~5 minutes
python scripts/nq_campaign.py --stage report   --name nq-<you>-01
```

**No hypothesis file? Give the statement instead.** Register needs exactly one of
`--hypothesis` or `--question`:

```
python scripts/nq_campaign.py --stage register --name nq-<you>-01 \
       --question "CVD divergence from price marks intraday trend exhaustion"
```

`scripts/write_hypothesis.py` writes the statement into a hypothesis, runs every
gate (lint, realised-outcome lint, grounding, length, and a review seat that must
return `NO_OBJECTION`), commits it marked `Machine-Written: nq-<you>-01`, and then
registers on it. Nobody reads it first, so under Amendment 15 the campaign may
spend trials but **may not support a claim** until a human has read the question.
If any gate fails nothing is registered; write it with
`write_hypothesis.py --question`, read it, and register on the file. In a session
plan, `question:` (or `seed_question:` for a family) does the same.

**`--hypothesis` is register-only, and it is written into `campaign.json`.** Every
later stage reads the path back out of the record and re-checks its digest, so a
resumed run cannot answer a different question from the one you registered — and
editing the markdown mid-campaign is refused rather than absorbed. Passing the flag
to any other stage is an error, not a no-op, because being ignored is how you come
to believe you changed the question.

**`validate` looks at the validation split exactly once**, with the campaign's gate
winner, and records section 5's three rules. It runs as `--purpose check` and does
**not** count toward N: the winner was already chosen on train, so it is not a look
taken while searching. Running it twice is refused. The multiplicity it does create
— one validation look per campaign — is corrected at L2 by `max_of_m`.

Rule (c), train-versus-validation, is **evidence for the committee and not a
threshold**. Do not treat a drop as a failure or a rise as a success on its own;
that is exactly the constant this project declines to fit.

`report` runs the four-auditor committee at the end and writes
`committee-verdict.json`, separately from `tier3-verdict.json`. If the gateway is
down, the committee records `ERROR` — which is **not** a pass. Under the MVP rule
(§6a) no veto means approve, and no verdict is not no veto.

**Name it after yourself.** `register` refuses to overwrite an existing record,
but only on your own disk — two people can both create `nq-mvp-02` and collide at
push time, and the loser's campaign has nowhere to go.

`run` is resumable: re-running it picks up at the next trial. A crash in hour
three does not cost hours one and two. Use `caffeinate -i` on a laptop.

**While `run` is going, leave the tree alone. Both directions bite.**

- *Do not run the test suite.* Each trial writes the candidate into
  `src/strategies/current.py` and restores it afterwards, so for most of a
  campaign that file is not the reference strategy. Tests that import
  `MomentumStrategy` from it fail to collect — three of them do. Nothing is
  broken and nothing needs fixing; you are reading a file mid-swap.
- *Do not edit anything under `src/core/` or `run.py`.* Every trial spawns a fresh
  process that re-imports them, so an edit lands in the middle of a campaign and
  the trials on either side of it ran different code. The ledger records a
  `code_fingerprint` per row and will show you the seam afterwards, which is worth
  something but is not worth the campaign.

Docs, `scripts/`, `audit/` and `tests/` are safe to edit — nothing a trial spawns
imports them.

**And do not add, remove or edit ANY file under a fingerprinted artifact path**,
even one this campaign does not use. `ARTIFACT_GLOBS` (`src/core/scoring/
integrity.py`) covers whole directories — `config/*.yaml`, `config/prompts/*.md`,
`config/hypotheses/*.md`, `data/calibration/*.json` and a few named files — and
the check compares the **whole set** at load against after the run. A file
appearing or disappearing anywhere in those globs invalidates the trial that was
in flight:

```
config/hypotheses/Test1.md: absent -> f06a6289ea3f
The score this run produced was not produced by the artifacts the row names.
```

That happened during `nq-orderflow-02`: an unrelated hypothesis file was dropped
into `config/hypotheses/` at trial 15 and cost that trial. The guard is right --
it refuses to attribute a score to an artifact state that did not hold for the
whole run -- but it is **broader than the dependency**: the campaign only reads
its own hypothesis, and a second file in the directory cannot affect its result.

Two practical consequences:

- Copy candidate hypotheses somewhere outside `config/` until you mean to
  register them. `/tmp`, a scratch dir, anywhere untracked and unfingerprinted.
- Once a stray file is there, **leave it** until the campaign ends. Deleting it
  is a second transition and costs a second trial.

An invalidated trial is recorded with `outcome: invalidated_artifacts_mutated`
and no score. It still occupies one of the N, which is correct -- the data was
looked at.

**On a machine short of memory, supervise it instead of babysitting it:**

```
nohup scripts/supervise_campaign.sh nq-<you>-01 50 >/dev/null 2>&1 &
disown
```

It restarts `--stage run` each time the process dies and stops after eight
attempts that advance no trials, so a campaign that cannot progress waits for you
rather than spinning all night. Progress goes to
`runs/campaigns/<name>/supervisor.log`.

This is not belt-and-braces. `nq-orderflow-01` was killed by a memory watchdog
four minutes in on a 16 GB laptop with 8 GB of swap already in use, and an
unattended campaign that dies at 02:00 has wasted the night. A kill costs the
trial in flight and nothing else: it never reaches the ledger, so it is not a look
at the data and does not count toward N.

Settings come from `config/run.yaml` under `campaign:` — N, `null_source`,
`correction`, q. Nothing about the evaluation is a command-line argument, for the
reason `run.py`'s docstring gives at length.

**`register` runs preflight first and will refuse to proceed if the gateway is
not serving the model the record names.** Let it refuse. The gateway carries a
wildcard fallback to `gemini-3.1-pro-preview`, and Task 37 lost 35 trials to
exactly that. A campaign that ran on a model its record does not name is void.

## 2a. Two ways the launch itself fails, both seen on 2026-09-25

Neither is a bug in the harness and both cost real time, so they are here rather
than in a review nobody reads.

### The environment does not reach the subprocess

`secrets/r2.env` holds bare `KEY=value` lines with **no `export`**. In zsh,
`source secrets/r2.env` sets shell variables that `run.py` — a child process —
never sees. `direnv`'s `dotenv_if_exists` exports them properly, so inside a
direnv-loaded shell this never happens; it bites when you launch from a script
or a shell where direnv did not run.

The failure is loud but misleading. `run.py` refuses at startup with

```
the NQ adapter needs read-only bucket credentials in the environment;
missing R2_ENDPOINT, R2_BUCKET. See docs/setup/R2_ACCESS.md.
```

and exits **before minting a ledger row**, so every trial comes back
`harness_failed: run.py produced no ledger row`. `harness_failed` is not a trial
(§3), so nothing is charged to N and nothing is lost except the proposal calls
already paid for — five, in the instance that prompted this. It reads like the
campaign is broken when the credentials are simply absent, and it looked partial
because two of the five R2 variables happened to already be in the environment.

```bash
set -a; source secrets/r2.env; set +a      # or just work inside direnv
```

### The gateway serves a different model than you asked for

```
ModelSubstituted: asked the endpoint for 'deepseek-v4-pro' and it answered
with 'gemini-3.1-pro-preview'.
```

The driver stops itself. **This is correct behaviour and the campaign is
usually resumable** — `ModelSubstituted` is raised inside `_one`, before the
reply is parsed and long before `run_trial` writes `current.py`, so the
proposal that hit it leaves no ledger row and no file behind. Check
`agent-calls.jsonl`: if every completed trial records
`models_served: ['<the model you registered>']`, the record is not false, the
guard fired *before* it could become one, and `--stage run` resumes at the next
trial with the budget intact.

The exception's own advice to "discard whatever has run" is for the other case —
trials that already ran on the wrong model. Read the sidecar before acting on it.

**Do not swap the model to get moving again.** It is frozen in `campaign.json`
and `stage_run` reads it from there; a different model is a different
experiment and needs its own registration.

**Why the check exists, concretely.** The gateway's `/models` names some of its
aliases in the id itself:

```
gpt-5-mini     (gemini-2.5-flash-lite)
gpt-5.6-luna   (gemini-2.5-flash-lite)
gpt-5.6-terra  (gemini-3.1-pro-preview)
```

and `gpt-5.6-luna` *also* appears unqualified. The same name can resolve two
ways, so only the `model` field on each response says what actually answered.
A model can also be listed and unroutable at the same time: within a minute of
the substitution above, `deepseek-v4-pro` began returning `HTTP 404, Cannot find
any route matching` while still appearing in the listing. That is the gateway
being reconfigured — wait for it rather than re-registering.

## 2b. A whole session, and a family that refines itself

The five commands above run **one** campaign. Two things run a session.

### A list of campaigns written in advance

```yaml
# config/sessions/mine.yaml
session: mine
campaigns:
  - name: nq-mine-01
    hypothesis: config/hypotheses/flow-continuation-5m.md
    n: 20
    model: deepseek-v4-pro
```

```
.venv/bin/python scripts/research_session.py --plan config/sessions/mine.yaml --dry-run
.venv/bin/python scripts/research_session.py --plan config/sessions/mine.yaml
```

Four phases, and **the order is the protocol, not tidiness**: every campaign
through `report` first, *then* validation for all of them at once, then the L2
gate over the declared family, then the holdout checked and not taken. §4's
defence against optional stopping is that a human deciding whether to run
campaign k+1 has seen no validation number, and that only holds if none exists
yet.

### A family that refines itself

```yaml
session: mine-02
family:
  seed: config/hypotheses/intraday-continuation.md
  iterations: 5
  n: 20
  model: deepseek-v4-pro
  prefix: nq-mine2
```

Campaign k+1 runs on a question written from campaign k's own outcome —
`scripts/refine_hypothesis.py`, machine-committed under **Amendment 11**, marked
`Machine-Written:` in its commit. **M is declared before anything runs; which
questions the campaigns ask is not.** That is the honest split: multiplicity is
the number of looks, and the number of looks is written down first.

Two things to know before you use it:

- **An unreviewed machine question may spend trials and may NOT support a
  claim.** If the family reaches L2 and passes, the pass is held until a human
  has read every machine-written question in it. Unattended running is allowed;
  unattended claiming is not.
- The refiner is not shown the gate's p-value, verdict, or any score, and it
  **refuses to run** if a validation artefact is readable. Don't try to "help" it
  by passing more.

### Stopping it

Ctrl-C once means *stop proposing and go to the stop* — no new campaign starts,
and the session validates every champion it has, runs L2, and checks the holdout.
It is a legitimate §4 stop precisely because phase 2 has not run, so there is no
validation number you could be reacting to.

Detached, there is no terminal to press Ctrl-C into, so the session prints its own
cue at startup:

```
  pid 98793 -- the cue is:  kill -INT -98793
```

**The minus before the pid matters.** It signals the process group, so the
campaign stops too; without it only the driver gets the signal and the child
`run.py` carries on orphaned. A second cue aborts.

### The holdout, if you mean to spend it

```
--holdout-if-l2-passes "why this look, in a sentence"
```

Only available **at launch**, and that is the point: at launch no validation
number exists, so the decision to spend the single look cannot have been made in
response to one. §8 gives 73 sessions one look **ever** — not one per session —
and `runs/holdout-result.json` existing is itself a refusal, so arming a second
session after spending it in the first changes nothing.

### Do not run the test suite while a campaign is running

A campaign owns `src/strategies/current.py`. `pytest` will fail to collect, which
is harmless — but several tests *write* that file, and one doing so mid-trial
means the campaign scores a source its record does not name. The ledger would
look clean and be wrong.

## 3. The accounting — read this before you run

**Commit your campaign directory.** N is the union over every committed ledger:
`src.core.runlog.global_n()`. A campaign sitting on your laptop is a set of looks
at the data that the project's trial count does not know about, which is the one
thing the ledger exists to prevent. Commit it even if the result is boring —
*especially* if the result is boring.

**Your campaign is exploratory unless it is the pre-registered one.**
`docs/design/pre-registration.md` §4: one pre-registered campaign is the
experiment, the rest are exploratory and are **not reportable as discoveries**.
That is not a demotion. Exploratory campaigns are how we decide what to
pre-register next, and they still count toward N.

**Do not report the best campaign across the team.** This is the important one.
If four of us run ten campaigns each and someone quotes the best of the forty, the
error rate of *that* procedure is not the p-value any single campaign printed —
it is the forking path one level up, and it is exactly what this project exists to
close. Reporting the best of many campaigns without the L2 correction would be us
committing, in our own work, the error we are writing a thesis about.

The L2 gate (`max_of_m` over campaign winners on validation) is the answer, and it
is not built yet. Until it is: run freely, commit everything, claim nothing.

**Nobody scores validation or the holdout.** The `nq-train` profile is
train-only, so this is structural rather than a rule you have to remember.
Validation is scored once, for all campaign winners at the same time, at the L2
stop. The holdout is 73 sessions and one look, ever, and `HoldoutLock` is one-way.

## 4. Reading the result

```
gate       p, and REJECT or ACCEPT
           accept = (p <= q) AND (observed_best > 0)
```

Both terms matter. A positive Sortino is not enough — the best of 50 worthless
candidates is positive most of the time. And a small p is not enough either:
under the sign-flip null a high-turnover strategy that *loses money* can clear it
decisively, because holding the trade count fixed makes its null
commission-dominated. `docs/design/pre-registration.md` §3b has the measurement.

**The measured FWER of 0.060 does not describe your campaign** unless you set
`null_source: conditional`. The default is `round_turn_mc`, which costs seconds
instead of ~12 hours and whose error rate nobody has measured. Every summary says
so in `error_control_note`. If a result ever looks worth claiming, re-run it under
`conditional` before claiming it.

**Expect tier 3 to say CANNOT_TELL on macOS.** Isolation is `best-effort` there —
the strategy process cannot be given a memory ceiling — so tier 3 escalates rather
than passing. That is the verdict working, not a failure. `docs/setup/LINUX_RUNNER.md`
is how to get a clean one.

## 5. What the first campaign found, so you know what normal looks like

`runs/campaigns/nq-mvp-01/`: 50 trials, best +0.0256, **p = 0.8236, REJECT**.

The agent cut turnover from 14,565 trades to 3 and the score improved the whole
way. The champion nets +0.6 index points — about $12 over eighteen months. It did
not find an edge; it found that when trading costs money and there is no edge, the
way to maximise the score is to stop trading.

If your campaign does something similar, it is not broken.
