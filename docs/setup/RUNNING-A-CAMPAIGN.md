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
