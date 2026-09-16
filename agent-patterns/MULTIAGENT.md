# Multi-Agent Patterns: Focused Subagent Personas for Accuracy and Deliverable Quality

Three composable multi-agent patterns, defined as portable markdown agents + skills.

The patterns are general — any harness with a "spawn subagent" capability can
execute them. The reference instantiation lives in this repository:

```
agents/    one persona per file (plus `errprobe`, a deliberate-failure fixture)
commands/  /design, /deliver, /committee — entry points for the three patterns
skills/    discipline packages the personas load on demand
```

For setup in this repo, see `AGENT-README.md`.

---

## Why focused personas

A single general-purpose agent asked to plan, implement, test, and judge its own
work fails in predictable ways: its context window fills with raw search output
(context rot), it inherits its own blind spots at verification time, and it can
quietly neutralize its own failures — a skipped test here, a loosened assertion
there — because the same agent holds every role. Narrowing each agent to a
focused persona attacks these failure modes structurally:

- **Fresh context per role.** Each subagent starts with a clean window holding
  only its brief. It reasons over the problem, not over the accumulated debris
  of every previous phase.
- **Verifier independence.** The agent claiming success and the agent checking
  it are always different agents — and should run **different model families**,
  so their failure modes don't correlate. Self-verification bias is the default
  behavior of every model; independence is the only cure.
- **Separation of duties.** Capability restrictions make dishonesty
  structurally impossible rather than merely discouraged: a coder with no shell
  cannot run — and therefore cannot fake — a green test run. Proving the change
  is the tester's *only* job.
- **Narrow prompts beat omniscient ones.** A short prompt with one job and
  explicit restrictions yields more reliable instruction-following than a long
  prompt covering every contingency.
- **Frontier asymmetry.** Cheap, fast models do the volume work; one frontier
  pass reviews the complete result as a safety net. Spend capability where it
  compounds — planning and final judgment — not on every keystroke.
- **Skills carry the discipline.** Personas stay small because standards live
  in skills loaded on demand (see [Skills](#skills-discipline-loaded-on-demand)):
  just-in-time expertise keeps context lean and makes the same rules fire every
  run, independent of what the base model happens to recall.

## The persona roster

A persona is defined by three things: its **lens** (what it optimizes for), its
**capabilities** (tools it may use), and its **restrictions** (what is denied —
each deny exists to make a specific failure mode impossible). This table
reflects the actual definitions in `agents/`:

| Persona | Pattern | Lens / purpose | Capabilities | Restrictions |
|---|---|---|---|---|
| `architect` | 1 | Plans and designs; interrogates the user (grilling) before planning | Read/grep/glob/webfetch + doc lookup; delegates broad exploration to `explore`; writes markdown | **Markdown only** — the edit/write fence allows `.md`/`.qmd`, never source code; no shell except read-only ticket-tracker queries (ticket writes ask the user) |
| `orchestrator` | 2 | Runs the build loop; coordinates only | Delegates to `coder`/`tester`/`test-reviewer`/`critic`/`explore`; writes the loop journal (`.scratch/**/*.md`); git branch/commit after a verified PASS | Never writes code, never "just fixes it"; worktree setup and ticket writes ask the user |
| `coder` | 2 | Implements exactly one step per invocation; minimal diff; mimics the patterns the brief cites | Read/edit/write; lint/typecheck commands | **No shell for builds or tests** — verification is the tester's job; no delegation; never stages or commits git |
| `tester` | 2 | Proves the change works — or proves it doesn't | Read/edit/write; shell limited to test-runner + lint/typecheck allowlists; runs the full suite | Bound by `test-guardian` honesty rules: no deleted/skipped/weakened tests — unfixable tests go on a blocked list with reasons |
| `test-reviewer` | 2 | Judge keeping the tester honest: independently re-runs everything | Test-runner allowlist (independent re-run), check-only lint modes, read-only git, `explore` for context gaps | Read-only: edit/write denied; never `--fix`/`--write`; verdicts from reproduced evidence only |
| `critic` | 2 | Frontier final gate: reviews the complete diff against the plan | Read-only git (`diff`/`show`/`log`/`status`) + file reads | Edit/write/task denied; every finding must cite inspected evidence (file, line) |
| `committee` | 3 | Chair: convenes the panel, anonymizes, judges, synthesizes | Delegates to the three panelists + `judge` + `synthesizer` + `explore` | Never answers the question itself; no edit/write; no shell |
| `panelist-pragmatist` | 3 | Lens: tradeoffs, operational cost, proven approaches | None — pure reasoning from the chair's brief | All tools denied |
| `panelist-skeptic` | 3 | Lens: failure modes, edge cases, hidden assumptions | None | All tools denied |
| `panelist-minimalist` | 3 | Lens: the simplest thing that could work | None | All tools denied |
| `judge` | 3 | Blind scorer: grades arguments, not identities; temperature 0 | None | All tools denied; receives anonymized responses only; may not explore to break ties — unresolvable-on-the-evidence is itself a verdict |
| `synthesizer` | 3 | Decider: merges the best elements, preserves real dissent | None | All tools denied; works only from provided materials |
| `errprobe` | — | Test fixture pinned to a deliberately nonexistent model so every call fails | — | Never dispatched in real runs; exercises subagent error surfacing |

Two structural rules cut across the roster:

- **Delegation is fenced.** Every persona carries an explicit `task` allowlist;
  only the coordinator/planner personas may delegate at all, and only to named
  collaborators — no delegation chains that escape the loop's structure.
- **Restrictions are load-bearing.** Each deny encodes a separation of duties
  (coder can't test, judge can't write, panelists can't wander), not generic
  paranoia. When adding a persona, name the failure mode each restriction
  prevents — if you can't, the restriction is probably wrong.

---

## Pattern 1 — Frontier planner/architect

**Goal:** the strongest available model plans; cheaper agents execute.

- The planner is **unpinned**: it inherits the user's model selection. Pick the
  strongest reasoning model available and the architect uses it.
- Permission fence: can only edit `*.md`/`*.qmd` — it writes plans and reports,
  never source code.
- Entry point: `/design <feature>` (or select the `architect` persona).
- Plans land in `docs/plans/YYYY-MM-DD-<slug>.md`, formatted so a no-judgment
  executor can run them: goal, non-goals, design decisions, ordered steps with
  acceptance criteria, testing requirements, explicit "do not guess" flags.
- Uses the **grilling** skill to interrogate the user one question at a time
  before planning; **grill-with-docs** additionally maintains `CONTEXT.md` +
  `docs/adr/` via **domain-modeling**.

The output contract is the point: the plan must be executable by an agent with
no architectural authority. Every judgment call either made it into the plan or
became a flagged open question — nothing is left for the executor to guess.

## Pattern 2 — Orchestrated build loop

**Goal:** plans get executed by less capable agents, with independent
verification at every step and a frontier model as the final safety net.

```
        ┌─────────────────────────────────────────────┐
        │  orchestrator (primary, user's model,       │
        │  coordinates only — never writes code)      │
        └─────────────────────────────────────────────┘
              │ per plan step, max 3 cycles:
              ▼
   coder ──► tester ──► test-reviewer ──FAIL──► back to coder
 (flash-    (flash-tier,  (different model      │ PASS
  tier)      runs suite)  family; re-runs       ▼
                          everything)      ... next step ...
                                                │ all steps pass
                                                ▼
                                          critic (FRONTIER tier)
                                                │ APPROVE
                                                ▼
                                          final report
```

| Persona | Mode | Tier | Role |
|---|---|---|---|
| `orchestrator` | primary | user's selection | Delegates everything; briefs are self-contained |
| `coder` | subagent | flash-tier executor | Implements one step per invocation, minimal diffs. No shell — verification is the tester's job |
| `tester` | subagent | flash-tier executor | Writes/runs tests under `test-guardian` honesty rules. Shell limited to test-runner + linter/typechecker commands |
| `test-reviewer` | subagent | mid/large verifier, different model family | Judge keeping the tester honest: independently re-runs the suite, hunts skips/deletions/weakened assertions, verdicts PASS/FAIL. Check-only linters + read-only git. Swap up at the first rubber-stamp |
| `critic` | subagent | frontier | Final gate: plan coverage, cross-step consistency, integration risk. Read-only git + file reads |

Per step, the default sequence is test-first: the tester drafts a detailed test
plan and shared scaffold (audited by the test-reviewer until approved), the
coder implements the step in small audited parts, the tester implements tests
one function at a time and runs the full suite, and the test-reviewer
independently re-runs and audits before the orchestrator commits.

Key invariants:

- **Verifier independence** — the agent claiming success and the agent checking
  it are always different agents *and different model families*.
- **Honesty gates** — the `test-guardian` skill: zero failures, no new skips,
  no deleted/weakened tests, blocked list instead of silent neutralization.
- **Frontier asymmetry** — cheap models do the volume work; one frontier pass
  (`critic`) reviews the complete diff against the original plan.

Entry point: `/deliver <plan-file-or-task>`.

## Tool gating (no purposeless shell)

Agents with unrestricted shell access wander outside their purpose. The default
is therefore **shell denied for every persona**; exploration happens through
built-in read-only tools — file read/grep/glob, web fetch, and a doc-lookup
skill/MCP for library documentation. Shell access returns only as narrow
allowlists for operations with no built-in equivalent:

- **Test execution** (verifiers only — `tester`, `test-reviewer`): the
  project's test-runner commands (npm/pytest/go/cargo/make-style runners, R
  testthat, Quarto renders). If a project's runner isn't allowlisted, agents
  report the exact command needed rather than work around it; extend the list
  via a project-level override of the agent file.
- **Linters/typecheckers** (`coder`, `tester`, `test-reviewer`): the same
  diagnostics the user sees in their editor, so the loop must verify them
  before claiming green. The reviewer runs **check-only modes** (never
  `--fix`/`--write`) to preserve its read-only role. CLI checkers are the
  verification channel that works without special harness flags; where a
  harness exposes an LSP diagnostics tool, it's the same signal on a second
  channel and verifiers hold both.
- **Read-only git** (judges — `test-reviewer`, `critic`):
  `status`/`diff`/`show`/`log`. Diff inspection is their core input.
- **Ticket tracker CLIs** (planner/coordinator): reads allowed, writes (create/
  edit/close/comment) always **ask** — tickets publish to the upstream tracker
  for hygiene, never silently.
- **Worktree setup** (coordinator only): **ask** — one worktree per parallel
  pipeline, with user consent.

Additional fences:

- **Delegation** (`task`) denied on every persona except the coordinator and
  chair, each with a named-collaborator allowlist — no delegation chains that
  escape the loop's structure.
- **Write fences mirror both tools.** File creation (`write`) is a separate
  capability from file modification (`edit`) with its own ruleset; fences must
  cover both or a "read-only" agent can still create files. The coordinator's
  sole write is the loop journal.

*In this repo:* these fences are frontmatter `permission` blocks in each agent
file (ordered broad-deny-first, narrow-allow-after).

## Exploration policy (delegated, or reported missing)

Exploration is always **delegated to a cheap, dedicated explorer persona** —
never done ad hoc by reasoning agents, who would burn their context windows on
raw search output.

- **Coordinating personas** (`architect`, `orchestrator`, `committee`): any
  question broader than a known file goes to the explorer with a concrete
  question and a thoroughness level. They read files directly only when they
  know exactly which file they need.
- **Executing personas** (everyone else; they cannot delegate): work from the
  brief plus the specific files it cites. If context is missing, they report
  `missing context: …` — the coordinator runs the explorer on the gap and
  re-briefs. Wandering the codebase is explicitly out of role.
- **Library/framework docs**: a doc-lookup MCP server (or skill) gives every
  persona — including no-shell loop agents — current documentation without
  shell access.

*In this repo:* the explorer is opencode's built-in `explore` subagent, pinned
to a tiny/free model so context management is cheap; swap the pin to taste.

## Pattern 3 — All-local committee

**Goal:** better answers to hard questions via diverse perspectives, blind
judging, and decisive synthesis — zero cloud cost.

```
 committee chair (primary, coordinates; grounds briefs via explore)
     │ one message, 3 parallel task calls:
     ├──► panelist-pragmatist — the Pragmatist (tradeoffs, operability)
     ├──► panelist-skeptic    — the Skeptic (failure modes, hidden assumptions)
     └──► panelist-minimalist — the Minimalist (simplest sufficient answer)
              │ answers anonymized A/B/C, order shuffled
              ▼
          judge (temperature 0)  ← optional adversarial pass for high stakes
              │ scores + winner + best elements
              ▼
       synthesizer (model-diverse from the entire panel)
              │ decides, doesn't average; preserves real dissent
              ▼
         final answer + per-panelist contributions + residual risks
```

Panelists, judge, and synthesizer are **tool-less reasoning personas**: they
see only the chair's brief and answer from it. Diversity comes from two axes —
different model families *and* different lenses per panelist.

Key invariants:

- **Diversity on two axes** — different model families *and* different lenses
  per panelist. Same-family panelists with different prompts correlate too much
  to be worth three dispatches.
- **Synthesizer family separation** — the synthesizer must not share a model
  family with any panelist or the judge it adjudicates; a shared family
  undermines independent judging. (This is a deliberate exception to the
  committee's otherwise-zero-cloud-cost goal when local rosters are
  single-family.)
- **Blind judging** — the judge never learns which model/lens wrote which
  answer; anonymize and shuffle before judging.
- **One or more judges** (following Karpathy's `llm-council`):
  - *Ordinary*: one judge pass, full scored report.
  - *High-stakes*: **peer ranking** — each panelist re-invoked with the
    anonymized answers (lenses off) plus the judge, all emitting strict
    `FINAL RANKING:` verdicts the chair aggregates by average position;
    optional adversarial pass on the aggregate leader.
- **Deciding, not averaging** — the synthesizer weighs answers, judging
  signals, and agreement/disagreement patterns together; dissent is preserved,
  not smoothed away.
- To run fully local end-to-end, pick a local model for the chair itself.

Entry point: `/committee <question>`.

---

## Skills: discipline, loaded on demand

Skills are how discipline attaches to personas without bloating every prompt.
A skill is a markdown instruction package an agent loads when the problem
context calls for it, not a permanent part of the system prompt:

- **Layered disclosure.** The `SKILL.md` entry point is thin; deeper references
  load only when needed (assertion design, root-cause tracing, UI prototypes).
  Context budget stays reserved for the actual problem.
- **Just-in-time expertise.** The right standard arrives at the right moment:
  honesty rules load when tests are written, review structure loads when an
  audit runs, interrogation technique loads when a plan has open questions.
- **Consistency.** The same discipline loads every run, regardless of what the
  base model recalls. The loop's guarantees are encoded in text, not hoped for
  from memory — which is why coordinators also **quote** skill constraints
  verbatim into briefs (see Loop mechanics).

| Skill | Source | Used by |
|---|---|---|
| `grilling` | mattpocock/skills (MIT), verbatim | architect, orchestrator, committee |
| `grill-me` / `grill-with-docs` | mattpocock/skills (MIT), verbatim wrappers | user-invoked (`grill me about…`) |
| `domain-modeling` (+ ADR/CONTEXT formats) | mattpocock/skills (MIT), verbatim | grill-with-docs sessions |
| `code-review` | generalized from awesome-copilot `se-security-reviewer` (MIT) | test-reviewer, critic |
| `test-guardian` | generalized from awesome-copilot `react19-test-guardian` (MIT) | tester |
| `codebase-design` (+ DEEPENING, DESIGN-IT-TWICE) | mattpocock/skills (MIT), verbatim | architect — deep-module vocabulary, parallel design-it-twice |
| `to-spec` | mattpocock/skills (MIT), lightly adapted (local `docs/specs/` default) | architect |
| `to-tickets` | mattpocock/skills (MIT), adapted: tracker-first publishing — `gh`/`glab` issues with blocking refs when in a repo, local files fallback | orchestrator intake |
| `handoff` | mattpocock/skills (MIT), verbatim | cross-session/cross-agent context transfer — orchestrator abort, blocked-step escalation, cross-harness portability |
| `prototype` | mattpocock/skills (MIT), verbatim (+ LOGIC.md, UI.md) | architect design-question detour — throwaway code to validate a state model or UI before committing to a plan |
| `improve-codebase-architecture` | mattpocock/skills (MIT), verbatim (+ HTML-REPORT.md) | surfaces deepening opportunities as a visual HTML report, then grills through the chosen one; pairs with `codebase-design` |
| `resolving-merge-conflicts` | mattpocock/skills (MIT), verbatim | standalone (user-invoked, not in the loop) — intent-based hunk-by-hunk conflict resolution for interactive merges; the loop aborts on conflict instead |
| `verification-before-completion` | obra/superpowers (MIT), verbatim | coder, orchestrator, critic — no unverified success claims |
| `systematic-debugging` (+ root-cause-tracing, defense-in-depth, condition-based-waiting) | obra/superpowers (MIT), verbatim | orchestrator's between-cycles debug pass |
| `receiving-code-review` | obra/superpowers (MIT), verbatim | coder — verify findings, fix or push back, never performative |
| `test-driven-development` (+ writing-good-tests) | obra/superpowers (MIT), verbatim | tester; red-first ordering in the loop |
| `using-git-worktrees` | obra/superpowers (MIT), verbatim | orchestrator — isolation for parallel steps |
| `dispatching-parallel-agents` | obra/superpowers (MIT), verbatim | orchestrator parallel fan-out; committee panel fan-out |

The awesome-copilot originals were framework-specific (React 19 migration;
security-only review); the installed versions keep the portable core — targeted
review plans, prioritized evidence-based findings, honesty gates, completion
gates, blocked-list escalation — and drop the framework specifics. The
`/design`, `/deliver`, and `/committee` commands are thin, explicitly-invoked
wrappers that hand off to the corresponding pattern personas.

## Loop mechanics worth knowing

- **Loop journal**: the orchestrator keeps canonical run state in
  `.scratch/<feature>/LOOP.md` (its only write permission) — every subagent
  report becomes a compact record, dispatches are briefed from plan + journal
  rather than conversational memory, and the final report is journal-driven.
  Compaction can no longer lose the run.
- **Briefing discipline**: every brief is self-contained (subagents see only
  what's in the prompt) and **quotes the skill constraints that matter** —
  honesty rules, report formats, red/green requirements — never relying on the
  subagent remembering to self-load. Local models drift; quoted constraints
  don't.
- **Test-first default**: per step, tests are planned and scaffolded before
  implementation. For logic-heavy steps and bug fixes the loop is strictly red-
  first: the tester writes failing tests from the brief, watched failing; the
  coder implements to green.
- **Debug pass**: a step that FAILs twice triggers a `systematic-debugging`
  pass — a root-cause statement is required before any third patch. Maximum
  3 cycles per step; then stop, write a handoff document, and surface the
  failure history to the user instead of flailing.
- **Parallel isolation**: independent steps may run in parallel, but only with
  one git worktree per pipeline; the orchestrator asks consent for
  `git worktree add` — decline and it runs sequentially. A merge conflict
  between pipelines aborts them and re-sequences the steps: conflicts indicate
  a planning failure, not a resolution problem.
- **Review protocol**: reviewer/critic findings flow back to the coder under
  `receiving-code-review` — verify, fix, or reasoned pushback; no performative
  agreement.
- **Git commit ownership** ("coders draft, reviewers audit, orchestrators
  commit"): subagents never stage or commit. Only the orchestrator commits, and
  only after an independent verification gate (test-reviewer PASS or critic
  APPROVE), using Conventional Commits referencing the plan step
  (`feat(step-3): implement remote-provision.sh`).

## Unattended-run hygiene

Subagents run without a human at the keyboard. Two rules keep unattended loops
from hanging or hallucinating progress.

### No permission resolves to "ask"

Any permission that defaults to interactive approval will hang a subagent
indefinitely — there is nobody to answer the prompt. Every subagent must
explicitly resolve ask-defaulting permissions to `allow` or `deny`:

| Concern | Subagent setting | Why |
|---|---|---|
| Tight identical-input loops | `deny` | break the loop instead of hanging |
| Reads outside the project | `deny` | subagents stay in-project |
| Asking the user questions | `deny` | subagents can't interact with the user |
| Secret files (`.env`) | `deny` | fail fast on secrets, don't hang |

**Principle: no subagent permission should ever resolve to an interactive
ask.** When adding a persona or a new permission key, audit for ask defaults.
The primary/coordinator persona may keep ask rules — it has a human.

### Silence is failure (empty-dispatch protocol)

Subagent delegation typically returns only the subagent's **final text
message**. If the subagent's model stream errors (provider 4xx/5xx, rate
limit, timeout, abort) before any text is produced, the caller receives an
**empty result with no error attached**. Treat any empty or whitespace-only
result as a FAILED dispatch — never as work done, PASS, or "nothing to
report":

1. **Evaluate** what actually happened (e.g. inspect the file the subagent was
   dispatched to write) and adjust the next brief accordingly.
2. **Retry once** with the same or updated brief, demanding a plain-text final
   report: "Your previous dispatch returned no text. Your entire report must be
   plain text in your FINAL message."
3. **Still empty?** Record it in the journal and surface it to the user — the
   model endpoint is almost certainly erroring. Check the harness log before
   re-running.

Infra failures are not patch cycles: they don't count against the 3-cycle step
limit, and silence never advances the loop.

## Model selection & swapping

Model choices are implementation details — **they live in agent frontmatter,
one `model:` line per persona, and swapping one never changes the pattern.**
The doc deliberately names no pins; choose from your harness's model list.
What transfers between projects is tier thinking:

| Tier | Personas | Selection principle |
|---|---|---|
| **Frontier planning/criticism** | architect, critic, (judge of last resort) | The strongest reasoning model available — this is the planning brain and the final safety net; capability here compounds |
| **Flash executor** | coder, tester | Small, fast, focused models. Precise, minimal-diff implementation from briefs is where a flash-tier model (e.g. Gemini Flash, a Haiku-class model) beats quantized local models |
| **Verifier** | test-reviewer | A **different model family** from the roles it audits; benefits from larger context and more turns to run the audit procedure. Swap up at the first rubber-stamp |
| **Local panel** | panelists, judge, (synthesizer) | Small local models for zero-cost, private diversity — diversity across *families*, not just prompts |

Selection principles:

- **Primaries stay unpinned** (architect, orchestrator, committee chair) so the
  user's model selector controls the planning brain and the loop's conductor.
- **Verifiers never share a family with the role they audit** — correlated
  failure modes defeat the audit.
- **Frontier asymmetry**: pay for the critic, save on the loop.
- **Committee diversity on two axes**: model family × lens. The synthesizer
  must differ in family from every panelist and the judge.
- **Temperature is a role knob**: verifiers and judges near 0 (deterministic
  verdicts), executors low, panelists higher (diversity).
- **Turn budgets are a reliability knob**: bound each persona's agentic turns
  per dispatch so a stuck subagent returns a blocked-report instead of
  spinning.
- **Per-project overrides beat global pins**: most harnesses let a project
  redefine a persona per-project — pin the coder to a project-appropriate
  model or adjust loop limits without touching the shared definitions.

## Portability (any harness)

The definitions are plain markdown + YAML frontmatter with no harness-specific
tooling in the bodies:

- **Bodies** (the prompts) are harness-agnostic — copy as-is.
- **Frontmatter mapping**: `description` is universal; subagent mode semantics
  and `model` ids map to the target harness's syntax; `permission` blocks map
  to each harness's tool gating (or drop them and rely on the body's
  discipline rules — weaker, but portable).
- **Skills** use the standard `SKILL.md` shape that Claude Code, pi, and
  hermes all scan.
- **Orchestration** is encoded in the orchestrator/committee prompts as plain
  instructions over a generic "spawn subagent" capability — any harness with
  subagents can execute the same loops.

## Attributions

- `grilling`, `grill-me`, `grill-with-docs`, `domain-modeling`, `codebase-design`, `to-spec`, `to-tickets` — [mattpocock/skills](https://github.com/mattpocock/skills), MIT License.
- `verification-before-completion`, `systematic-debugging`, `receiving-code-review`, `test-driven-development`, `using-git-worktrees`, `dispatching-parallel-agents` — [obra/superpowers](https://github.com/obra/superpowers), MIT License. (Its `docs/porting-to-a-new-harness.md` is also the best reference when porting these patterns to other harnesses.) `requesting-code-review` was reviewed and removed — redundant with the loop's fixed critic/test-reviewer roles; its useful logic (severity triage, deviation-intent checks, praise-first calibration) was folded into the critic and orchestrator personas.
- `code-review`, `test-guardian` — adapted from [github/awesome-copilot](https://github.com/github/awesome-copilot) agent definitions, MIT License.
- Committee peer-ranking mode and the `FINAL RANKING` verdict format — patterns from [karpathy/llm-council](https://github.com/karpathy/llm-council) (provided "as is for inspiration", no license file; concepts re-implemented here, no code copied).
