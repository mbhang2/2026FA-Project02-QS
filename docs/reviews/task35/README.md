# Task 35 — `agent-patterns/` absorbed, and mapped to this project

Standalone. Nothing in the repo changed: no agent definitions, no `permission:`
blocks, no workflow switched. This document is the whole deliverable.

What was read: `agent-patterns/MULTIAGENT.md` (443 lines), `AGENT-README.md`, all
14 files in `agents/`, all 3 in `commands/`, and all 24 skills under `skills/`.

---

## 1. Inventory

Fourteen agent definitions. `model`, `temperature` and `steps` are extracted from
the frontmatter rather than from the prose, because two of them disagree (§6).

| name | what it does | permission posture | delegates to |
|---|---|---|---|
| `architect` | Plans and designs; grills the user before planning; writes executable plans to `docs/plans/`. Unpinned — inherits the user's model. | `edit`/`write`: deny `**/*`, allow `*.md`/`*.qmd`, then re-deny `node_modules/**` and `.git/**`. `bash`: deny `*`, allow read-only `gh`/`glab`, `ask` on issue writes. `read`: deny `*.env`. | `explore`, `architect` (self, for design-it-twice), `critic`, `committee` |
| `orchestrator` | Conducts the build loop; commits verified steps; keeps the run journal. Unpinned, `mode: primary`. | `edit`/`write`: deny `**/*`, allow `.scratch/**/*.md` only. `bash`: deny `*`, allow `git status/diff/add/commit/branch/checkout/switch`, `ask` on `worktree`/`revert`/`merge` and issue writes. | `coder`, `tester`, `test-reviewer`, `critic`, `explore` |
| `coder` | Implements exactly one plan step per invocation; minimal diff; no architectural authority. `gemini-3.7-flash`, temp 0.15, 20 steps. | `edit`/`write`: **default allow, deliberately** ("it must create source files"). `bash`: deny `*`, allow linters/formatters/typecheckers only. `task: deny`. | — |
| `tester` | Writes and runs tests; owns the detailed test plan and scaffold; reports counts. `claude-haiku-4-5`, temp 0.1, 30 steps. | `edit`/`write`: default allow (must create test files). `bash`: deny `*`, allow test runners + linters. `task: deny`. | — |
| `test-reviewer` | Independently re-runs the suite and audits the tester for gaming; verdicts PASS/FAIL. `deepseek-v4-flash`, temp 0, 20 steps. | `edit`/`write`: deny. `bash`: test runners, check-only linters, read-only git. | `explore` |
| `critic` | Frontier final gate on the complete diff against the original plan. `gemini-3.1-pro-preview`, temp 0.2, `thinkingLevel: high`. | `edit`/`write`/`task`: deny. `bash`: deny `*`, allow `git status/diff/show/log`. | — |
| `committee` | Chair: frames the question, fans out to three panelists, anonymizes, judges, synthesizes. Unpinned, `mode: primary`. | `edit`/`write`: deny. `bash`: deny. | `panelist-{minimalist,skeptic,pragmatist}`, `judge`, `synthesizer`, `explore` |
| `panelist-pragmatist` | Lens: tradeoffs, operational cost, proven approaches. `Qwen3.6-35B` local, temp 0.4. | Every tool denied. Answers only from the chair's brief. | — |
| `panelist-skeptic` | Lens: failure modes, edge cases, hidden assumptions. `Qwen3.6-27B` local, temp 0.4. | Every tool denied. | — |
| `panelist-minimalist` | Lens: the simplest thing that could work. `Gemma-4-26B` local, temp 0.4. | Every tool denied. | — |
| `judge` | Blind scorer over anonymized A/B/C responses; report mode or strict `FINAL RANKING`. `Ornith-1.0-35B` local, temp 0. | Every tool denied. May not explore to break ties. | — |
| `synthesizer` | Decides (not averages) from panelist answers + judge reports; preserves dissent. `deepseek-v4-flash`, temp 0.2. | Every tool denied. | — |
| `errprobe` | Fixture pinned to a nonexistent model so every call fails; tests that subagent LLM errors surface to the caller. | Everything denied. Carries `claude.skip`/`codex.skip`/`copilot.skip`. | — |
| `interactive` | Safety-first primary that routes every action through harness approval. Unpinned. | Everything `ask` except read/glob/grep/list/todowrite/lsp. The only file with **no `name:` key** and the only one that uses `ask` as a subagent-visible default. | `ask` (unfenced) |

Three commands (`/design` → architect, `/deliver` → orchestrator, `/committee` →
chair) are thin wrappers: frontmatter is `description` + `agent`, body is the
prompt with `$ARGUMENTS`.

Twenty-four skills. Frontmatter is `name` + `description`, plus
`disable-model-invocation: true` on 11 of them (user-invoked study aids that the
model may not auto-load) and `argument-hint` on one. Provenance is attributed per
skill in `MULTIAGENT.md` — mattpocock/skills, obra/superpowers, and
github/awesome-copilot, all MIT, most installed verbatim.

---

## 2. The conventions

### The frontmatter schema, as actually used

Top-level keys observed across the 14 agents, with counts:

```
permission 14   mode 14   description 14   name 13   temperature 10
model 10        codex 10  claude 10        steps 3   thinkingConfig 1   copilot 1
```

`mode` takes `primary` (user-selectable entry point), `subagent`
(dispatch-only), or `all` (both — `architect` is the only one). Omitting `model`
is meaningful, not lazy: it means *inherit the user's selection*, and all three
coordinators do it on purpose so the human's model-selector choice controls the
planning brain.

`claude:` / `codex:` / `copilot:` are per-harness override blocks carrying that
harness's `model` and reasoning-effort key, plus `skip: true` for agents with no
meaningful port. This is the portability mechanism: one file, three harnesses.

`steps` bounds agentic turns per dispatch. `MULTIAGENT.md` calls it a reliability
knob — "a stuck subagent returns a blocked-report instead of spinning."

**A key the example did not show:** the permission map is open-ended. Sixteen
distinct sub-keys appear across the roster — `edit`, `write`, `bash`, `task`,
`read`, `doom_loop`, `question`, `external_directory`, `lsp`, `webfetch`,
`websearch`, `todowrite`, `skill`, `list`, `grep`, `glob`. Twelve of the fourteen
agents set exactly the same eight; `interactive` is the outlier that exercises
the rest.

### Last-match-wins, and the failure it prevents

Rules resolve **last match wins**, so ordering is semantic, not stylistic. Two
consequences are hard-coded into the files with comments explaining them:

1. **The broad deny must come first.** `architect` and `orchestrator` both write
   `task: {"*": deny, <names>: allow}` with a comment saying the reverse order
   denies everything. Same for `bash`.
2. **Broad allows must be re-denied for sensitive trees.** `architect` allows
   `**/*.md`, which would otherwise outrank the global deny and let it edit
   `node_modules/**/*.md` and anything markdown under `.git/`. It re-denies both
   *after* the allows. The failure prevented is specific: an allow written for a
   good reason silently re-opening a tree that a deny written earlier had closed.

This is the one convention in the set that is purely mechanical and purely
load-bearing. It is also the one most likely to be copied wrong, because the
correct order looks redundant.

### `write` and `edit` are different tools

File creation and file modification are separate capabilities with separate
rulesets. A fence on one is not a fence on the other, in either direction:
`architect` fences both identically or plan creation is blocked; `orchestrator`
fences both to `.scratch/**/*.md` or a "journal-only" agent can still create
files anywhere. `coder` and `tester` leave both at default allow, with a comment
saying so deliberately.

### Delegation is fenced, and the fence is an allowlist

Only the three coordinators may delegate at all, each to a named list of its own
collaborators plus `explore`. Everyone else has `task: deny`. The stated purpose
is that there are no delegation chains that escape the loop's structure — a
subagent cannot acquire capability by spawning something less restricted.

### Unattended-subagent hardening, and the trade-off they state

Every subagent sets `doom_loop: deny`, `question: deny`,
`external_directory: deny`, and `read: {"*.env": deny}`. The reasoning is
mechanical rather than moral: **any permission that resolves to `ask` hangs a
subagent forever**, because there is nobody at the keyboard to answer. So every
ask-defaulting key must be explicitly resolved to allow or deny.

The trade-off is stated rather than hidden, in `architect`'s comment. `architect`
is `mode: all` and recurses into itself for design-it-twice, so its nested
instances are unattended while its top-level instance is not. A static permission
block **cannot distinguish a top-level human-present invocation from a nested
self-clone**, so both resolve the same way, and interactive top-level use loses
the ability to ask-then-allow an external-directory read. They accept the loss and
name the workaround (use a different agent for that read).

That is the most honest thing in the pattern set, and it generalizes: *a static
declaration cannot condition on the dynamic context of the call.* It is the same
shape as the finding in §3.

### Other conventions that read as hard-won

- **Shell is denied by default for every persona**, and returns only as narrow
  allowlists for operations with no built-in tool equivalent — test execution,
  linters, read-only git, ticket reads. "Agents with unrestricted shell access
  wander outside their purpose."
- **Verifier independence, on two axes.** The agent claiming success and the agent
  checking it are never the same agent, and should not be the same *model family*,
  because correlated failure modes defeat the audit.
- **Separation of duties as capability, not instruction.** "A coder with no shell
  cannot run — and therefore cannot fake — a green test run." The restriction is
  what makes the honesty structural.
- **Silence is failure.** A subagent whose model stream errors returns an *empty
  result with no error attached*. Any empty or whitespace-only result is a FAILED
  dispatch, never work done. Retry once demanding plain text in the final message;
  still empty, record it and surface it. Infra failures do not count against the
  3-cycle step limit.
- **The report must be the FINAL message.** Repeated in four files, with the
  reason: the caller receives only the last text message, so a report followed by
  "I'm done" loses the report.
- **Quote skill constraints into briefs verbatim** rather than trusting the
  subagent to self-load them. "Local models drift; quoted constraints don't."
- **The loop journal** at `.scratch/<feature>/LOOP.md` is the orchestrator's only
  write and its external memory, because "context is volatile — compaction can
  wipe it at any time." Dispatches are briefed from plan + journal, never from
  conversational memory.
- **3-cycle limit, then a handoff document**, not a fourth patch. Two consecutive
  FAILs trigger a `systematic-debugging` pass that requires a root-cause statement
  before any new patch.
- **Commit ownership**: "coders draft, reviewers audit, orchestrators commit."
  Subagents never stage or commit; the coordinator commits only after an
  independent PASS or APPROVE.
- **Temperature is a role knob** — 0 for judges and verifiers, 0.1–0.2 for
  executors, 0.4 for panelists where diversity is the product.

---

## 3. Mapping to this project

### a. The loop's strategy agent

Here is what the definition would look like. It is a draft for discussion, not a
file to install, and §4 is the reason.

```yaml
---
name: strategy
description: Proposes one candidate trading strategy per invocation by writing
  src/strategies/current.py. Cannot evaluate it, score it, or learn its score.
mode: subagent
temperature: 0.7          # proposals are the one place diversity is the product
steps: 12
permission:
  # The write path, mirrored across both tools.
  edit:
    "**/*": deny
    "src/strategies/current.py": allow
  write:
    "**/*": deny
    "src/strategies/current.py": allow
  # One command, and it executes nothing. run.py is not on this list.
  bash:
    "*": deny
    "python scripts/agent_check.py": allow
  task: deny
  # Everything the gate is built out of.
  read:
    "src/core/scoring/**": deny
    "data/calibration/**": deny
    "config/calendars/**": deny
    "runs/**": deny
    "docs/reviews/**": deny
    "docs/design/metric.md": deny
    "*.env": deny
  webfetch: deny
  websearch: deny
  # Unattended hardening, per their convention.
  doom_loop: deny
  question: deny
  external_directory: deny
---

You write one strategy per invocation, to src/strategies/current.py.
Read docs/agent/strategy-api.md and docs/agent/data.md; they are your
complete interface. `python scripts/agent_check.py` tells you whether the
file is well formed. It reports no number, runs no data, and a runtime
error costs a trial — see §7b of the API document.
```

**What the schema can express.** Four of our constraints map cleanly, and the
mapping is genuinely useful as documentation: the single writable file, mirrored
across `write` and `edit`; the single callable command; `task: deny`; and the
read-denies over the scorer, the null tables, the calendar and the ledger. The
unattended hardening transfers verbatim and we would want it.

**What the schema cannot express — and this is most of the fence.**

1. **The import prohibitions.** Nothing in this vocabulary is about a Python
   module graph. `read: {"src/core/scoring/**": deny}` stops the *authoring
   session* from opening the scorer. It says nothing about whether the
   `current.py` that session produces can `import src.core.scoring.scorer` when
   the harness runs it an hour later. **The permission block governs the agent's
   tool calls; our boundary has to govern the artifact's runtime behaviour.**
   Those are different objects, and conflating them is the single most dangerous
   reading of this schema for this project.
2. **Network egress from the strategy process.** `webfetch: deny` /
   `websearch: deny` stop the agent from browsing while it writes. They have no
   bearing on `import socket` inside `current.py`. Neither adversarial pass caught
   network egress and it is the attack that defeats every other defence we have:
   genuine blotter, stable dataset digest, clean fingerprint, and a replay that
   reproduces because it fetches the prices again.
3. **Resource ceilings.** There is no key for `RLIMIT_AS` or `RLIMIT_CPU`. A
   strategy that allocates without bound is a harness failure, not a denied
   action.
4. **"Every invocation leaves a ledger row."** This is accounting, not
   permission. There is no way to say *this action must be recorded* — only
   allow, deny, or ask.
5. **"The agent may not learn how it is judged."** This is an information-flow
   property. `bash: {"python scripts/agent_check.py": allow}` is expressible;
   *and the output of that command must not be a function of NQ prices* is not.
   Task 23 is the whole argument: the second design of that check reported no
   number, was expressible in exactly this schema, and leaked peak realized P&L to
   ±0.0002 in eighteen probes with zero trials counted.
6. **N.** Nothing in the schema counts or bounds invocations, and honest N is the
   project's contribution.
7. **The digest-bound in-process exception.** `RunTicket.for_calibration` is
   allowed because it checks the source digest against the committed reference.
   "Allowed only when this hash matches" has no expression here.
8. **Determinism and seed control.** Not a permission concept at all.

Scoring that: the schema carries roughly the first layer of a four-layer fence,
and the three layers underneath it are the ones that actually hold under an
adversary.

### b. The committee

Both patterns named in the brief exist. `critic` is a single frontier reviewer
with an evidence standard — "every finding cites evidence you actually inspected
— file, line, and what you saw — or it doesn't ship" — plus severity calibration
and a required *Residual risks* section for what it could not verify from the
diff. `committee` is a chair that fans out to three tool-less panelists with
generic dispositional lenses (Pragmatist / Skeptic / Minimalist), anonymizes
their answers as A/B/C, has a blind `judge` score them, and hands everything to a
`synthesizer` that "decides, doesn't average."

**Our committee is not a special case of theirs, and the difference is the
operator.** Theirs converges: three views in, one best answer out, with the judge
picking a winner and the synthesizer merging the best elements of the losers.
Ours is conjunctive: four domain auditors — leakage, statistics, economics,
robustness — and **any one of them can veto**. Synthesis is precisely the wrong
operation for a veto gate; a synthesizer that merges a leakage auditor's "this
candidate sees the future" with three approvals produces a nuanced paragraph
where the correct output is *rejected*. The `judge`'s ranking mode makes it
worse: it forbids ties and forces a winner, which is an aggregation primitive
where we need an AND.

There is a second mismatch. Their panelists are *dispositions* and are
deliberately tool-less; three of our four auditors are substantially
*computations* — purged/embargoed CV, DSR against the global N, PBO via CSCV,
Harvey t > 3.0. Those are code that returns a number, not a lens that returns an
opinion. An LLM panel is the wrong mechanism for CSCV.

What is reusable, and it is not nothing:

- The chair's fan-out mechanics: one message, parallel dispatch, **self-contained
  briefs** (panelists see only what the chair sends), grounding delegated to an
  explorer rather than spelunked in the chair's own context.
- The **strict machine-parseable verdict**. `FINAL RANKING:` exists so the chair
  can aggregate mechanically instead of interpreting prose. Our version is
  `VERDICT: VETO | PASS` plus a cited-evidence block, and we want it for the same
  reason.
- `critic`'s evidence standard and its *Residual risks* section, which is the
  same discipline as our "every verification states its replication count and
  what effect size it can resolve."
- The chair's rule that **a concern raised independently by every panelist gets
  surfaced prominently even if the synthesis resolves it.**
- Blind judging, adapted: not anonymizing *auditors* (their identity is their
  domain and is load-bearing), but withholding the candidate's score from the
  auditors, which is the same principle applied to the thing that would actually
  bias them.

Verdict: reuse the mechanics, replace the operator. Four dispositional lenses are
not four domain audits, and a synthesizer is not a veto.

### c. Our own workflow

The correspondence is closer than expected. Thirty-four tasks have run as:
human writes a brief with explicit report-back bullets → I do the work → full
suite → commit with a detailed message → report against the bullets. That is
`architect` → `orchestrator` → `tester`/`test-reviewer` → `critic`, with the
planner played by a human and the verification played by pytest.

The two substitutions are deliberate and I would keep both.

**Our planner is a human, and the brief is a pre-registration.** The briefs
encode decisions the project will not let an agent make: *do not tune a threshold
to make a curve look better*, *do not touch validation or holdout*, *state the
replication count and the resolvable effect size before the measurement runs*.
`architect` would extract those by grilling — "facts you look up yourself;
decisions belong to the user" is the same principle — but a plan an agent wrote
after seeing the data is not a pre-registration, and `docs/tasks/taskNN-*.md`
committed before the work is. The dated artifact is the point.

**Our verifier is mechanical, not a second model.** `MULTIAGENT.md` argues
verifier independence from model diversity: the checker must be a different
family or its failure modes correlate. Our answer to the same problem is
determinism — 548 tests, golden blotter digests asserted twice, a test that fails
when `architecture.md` stops naming a module, a ledger-integrity test that
re-derives every committed row. A test does not have correlated failure modes
with the author; it has none. Where we *do* need judgment rather than a
predicate — the adversarial passes — we already use an independent pass and keep
its scripts in `docs/reviews/second-pass/`.

**What we would gain by adopting parts of it:**

- **The loop journal.** `.scratch/<feature>/LOOP.md` exists because "compaction can
  wipe context at any time." That happened in this session. A per-task journal
  holding the brief verbatim, the step list, and one compact record per completed
  piece is a direct remedy and costs nothing.
- **`verification-before-completion` as a named gate.** "If you haven't run the
  verification command in this message, you cannot claim it passes." We follow
  this; we have never written it down as a rule with a name.
- **The empty-dispatch protocol.** "Silence is failure" is the same principle as
  our "a run that happened and left no trace is what the ledger exists to
  prevent," applied to subagent results. If we ever run a campaign through
  dispatched agents, this is the rule that stops an infrastructure error from
  being recorded as a completed trial — which would corrupt N in the one place
  nobody would look.
- **The 3-cycle limit and a handoff document instead of a fourth patch.** Task 23's
  first attempt and Task 33's first hypothesis were both abandoned correctly; a
  stated limit makes that the rule rather than the judgment call.
- **Structured report formats.** Fixed templates with counts, `Deviations &
  concerns`, and `Residual risks` sections.

**What we would lose by adopting the whole loop:** the brief-as-pre-registration,
if an orchestrator re-plans mid-run; the ability to run at all through the
gateway, which the README says blocks Opus and Sonnet while most pins here are
LiteLLM ids; and four dispatches per step against a suite that takes 68 seconds to
answer the same question definitively.

---

## 4. The caveat, in the form it would go in the repo

> ### Permission blocks are a fence, not a boundary
>
> `agent-patterns/` expresses an agent's limits as a declarative `permission:`
> block: allow these paths, deny those, no shell, no delegation. It is a good
> vocabulary and this project can use it — for **describing** the research
> agent's fence, for the ergonomics of a dispatched loop, and for keeping an
> agent inside its role.
>
> It is not what stops our strategy agent, and nothing in it should be read as
> making the isolation layer redundant.
>
> **What the pattern language can carry:** role clarity, tool scoping, delegation
> limits, separation of duties between cooperating agents, the unattended-run
> hygiene (no permission resolving to `ask`, silence is a failed dispatch), and a
> readable statement of intent that a human can audit at a glance.
>
> **What it cannot carry:** anything the strategy must not be able to do *while it
> is trying to*. Specifically: the import prohibitions, because the block governs
> the authoring session's tool calls and not the runtime behaviour of the file it
> writes; network egress from the strategy process; a memory or CPU ceiling; the
> requirement that every invocation leaves a ledger row; the requirement that no
> channel's output be a function of market data; and any rule conditioned on a
> digest rather than a path.
>
> This is not a theoretical concern. Three adversarial passes against this repo
> each closed one in-process containment path and each was followed by another:
> `ctx._engine`, then `__self__` through `_SealedDataset.__getattr__`, then
> `__closure__` into the mutable `_RunState` — the last of which moved Sortino
> from −0.2547 to +4.2089. What finally worked was not a better list. It was a
> subprocess with a read-only bind mount, denied network, `RLIMIT_AS`, and a
> module allowlist that fails by name. A path allowlist stops an agent from
> wandering; it does not stop code that is trying to get out, because by the time
> the strategy runs, the agent that was fenced is no longer the thing executing.
>
> Two properties of the pattern language make the confusion easy, and both are
> visible in `agent-patterns/` itself:
>
> - **A shell allowlist that admits a test runner admits arbitrary code.**
>   `tester` denies `bash: "*"` and then allows `make*` and `pytest*`. Both
>   execute project code by construction — a Makefile target, a `conftest.py`.
>   The fence reads as narrow and is not. Our own Task 23 is the same lesson from
>   the other direction: a check that executed the strategy and reported only
>   pass/fail looked like it carried no information, and eighteen probes read
>   peak P&L off it to ±0.0002.
> - **Capabilities compose across agents even when no single agent holds both
>   halves.** `coder` may write any file and may not run tests; `tester` may run
>   tests and audits only what the brief names. Neither alone can execute
>   arbitrary code from a written file; in sequence they can. Reasoning about each
>   block in isolation is not reasoning about the loop.
>
> Adopt the vocabulary. Keep the kernel.

---

## 5. Recommendation

**Adopt the conventions. Adopt no agent into the scored path. Adopt nothing as a
substitute for isolation.**

Concretely, in rising order of cost:

1. **Write the strategy agent's `permission:` block into the docs, as
   documentation** — beside `docs/agent/strategy-api.md`, with §4's caveat
   attached to it in the same place. Cost: an hour. Benefit: the fence becomes
   legible in one screen instead of being reconstructed from four mechanisms in
   three documents. Risk, and it is the reason §4 has to travel with it: someone
   reads the tidy block and concludes the rest is belt-and-braces.
2. **Adopt four disciplines by name**, in `CLAUDE.md`: no completion claim without
   fresh verification evidence; a task journal that survives compaction; silence
   is a failed dispatch; three cycles then a handoff, not a fourth patch. Cost:
   an afternoon of documentation. Benefit: three of the four are already how this
   project works, and writing them down is what made the block-length decision
   and the no-inflation-factor decision stick.
3. **Borrow `critic`'s report shape for the next adversarial pass** — severity
   calibration, every finding citing file/line/what-was-seen, and an explicit
   *Residual risks* section for what could not be verified. Cost: nothing. Our
   second-pass README is already close; the residual-risks section is the part
   it lacks.
4. **Do not adopt `committee` as our committee.** Reuse the fan-out mechanics and
   the machine-parseable verdict; replace the judge-plus-synthesizer with a
   conjunctive veto, and implement the three statistical auditors as code.
5. **Do not adopt the build loop for harness code**, at least not before the
   committee tier exists. Our verifier is a 68-second deterministic suite and our
   planner is a pre-registration. The loop's value is highest where verification
   is expensive and judgment is cheap; here it is the other way round.

---

## 6. Where the patterns contradict a decision this project has already made

The interesting half. Each of these is checkable in the files.

**1. `coder` and `tester` leave `edit`/`write` at default allow — deliberately.**
The comment says "intentionally left at default allow — it must create source
files." Our permission model is the inverse: a whitelist on the write path,
`src/strategies/**` and nothing else, with a CI check that compares changed paths
against the allowlist. Their two most write-heavy agents have the *least* fenced
write permission in the roster. The reasoning is coherent for their threat model
— a cooperating agent that should not wander — and it is exactly wrong for ours,
where the writer is the party being measured.

**2. `critic` shares a model family with `coder`.** `MULTIAGENT.md` states the
invariant twice: "the agent claiming success and the agent checking it ... should
run **different model families**, so their failure modes don't correlate," and
"Verifiers never share a family with the role they audit." The pins are
`coder: litellm/gemini-3.7-flash` and `critic: litellm/gemini-3.1-pro-preview`.
Same family. The critic is the final gate on the coder's diff.

The interesting part is not the slip; it is that **nothing detects it.** The
invariant lives in prose and the pins live in frontmatter, and no check compares
them. This repo hit the identical class of defect and fixed it structurally:
`tests/test_architecture_doc.py` exists because the scripts table in
`architecture.md` stopped growing when `session_closes.py` was added, and
`integrity.artifact_digests()` exists because a fingerprint that does not cover
the file it claims to cover is a fingerprint that reports clean while the null
table changes underneath it. A stated invariant with no test is a comment.

**3. Permission is a prefix glob, and the shell allowlist reopens what the file
fence closed.** `orchestrator` denies `edit`/`write` on `**/*` except
`.scratch/**/*.md`, then allows `git checkout*` and `git commit*`.
`git checkout <ref> -- <path>` writes arbitrary tracked files; `git commit` runs
whatever is in `.git/hooks/`. The declared write fence and the shell allowlist
disagree, and the shell allowlist wins. This is the same structural failure as
finding 2 of our second pass — a seal that forwards through a channel nobody
checked — and it argues for our standing position that the mechanism which holds
is the one where the capability is *absent*, not the one where it is *listed as
denied*.

**4. Run state is disposable there and evidence here.** The orchestrator's
journal lives in `.scratch/`, which is gitignored, and the README says files
there "may be deleted at any time." Our equivalent — `runs/ledger.jsonl`,
`runs/fills/`, `runs/campaigns/`, `runs/sources/` — is append-only and
**committed**, because a trial the record cannot reproduce is a trial that did
not happen, and deleting rows afterwards is a rewrite of the record by the party
being measured against it. Same artifact, opposite lifecycle, and both are right
for their purpose. Worth naming so nobody helpfully moves our journal into
`.scratch/`.

**5. The judge may not return a tie.** Ranking mode: "Never rank two responses
equal." Report mode is better — it has an *Open disagreement* section and says
"unresolvable-on-the-evidence is itself a verdict" — so the file contradicts
itself depending on mode. Our position is settled and is the report-mode one:
Task 34 measured a bias of +0.021 at R = 20 and declined to claim anything from
R = 150 upward because the effect was inside the noise the design could resolve.
Forced discrimination on insufficient evidence is the failure this project exists
to prevent, and a ranking primitive that forbids "cannot tell" has it built in.

**6. `docs/plans/` means something different in each.** `architect` writes to
`docs/plans/YYYY-MM-DD-<slug>.md`. Ours holds three files — the harness plan,
one historical proposal, and a README saying plans are artifacts and not
authoritative for current capabilities — and our task briefs go to `docs/tasks/`. Adopting `/design` as-is would scatter agent-written plans into
the directory the build plan lives in. Trivial to avoid, easy to miss.

**7. The install instruction makes these agents global, not project-scoped.**
`AGENT-README.md` has students symlink `agent-patterns/{agents,skills,commands}`
into `~/.config/opencode/`. Every permission block in them is written relative to
the agent's working directory, so `"**/*": deny` plus `"**/*.md": allow` follows
the user into every other repository on their machine. That is presumably
intended — they are meant to be portable personas — but it is worth knowing
before anyone writes a fence here that assumes it only applies here.

**8. One definition is missing its `name:`.** `interactive.md` has no `name` key
where the other thirteen do, and it is also the only file that uses `ask` on
subagent-relevant keys. Whether the harness falls back to the filename is a
harness question; either way it is the sort of drift a schema check would catch
and prose will not.
