# Multi-Agent Architecture for OpenCode and IDE Harnesses

Three composable multi-agent patterns, defined as portable markdown agents + skills.

**Where this lives:** The canonical, git-tracked home is `ai-agent-config/` in
the `devenv` repo (this file sits alongside the agents it documents).

```
agents/          13 canonical agent definitions (12 production + 1 test fixture `errprobe`)
commands/        /design, /deliver, /committee — canonical entry points
skills/          grilling, grill-me, grill-with-docs, domain-modeling, code-review, test-guardian
```

Generated directories (`claude-agents/`, `claude-commands/`, `codex-agents/`, `codex-prompts/`, `copilot-agents/`, `cursor-agents/`, `cursor-hooks/`, `cursor-skills/`, `zed-skills/`) are mechanically derived from these canonical files and tracked in git for zero-build provisioning across environments.

### Synchronization & Generation Commands

To regenerate harness configs and establish local symlinks into `~/.<harness>/`:

```sh
cd node-manager
make agent-config-symlinks
```

- **Localhost workflow:** Run `make agent-config-symlinks` from your primary canonical repo checkout. (*Note:* Do not run this from a temporary git worktree unless you intend for local symlinks in `$HOME` to target that specific worktree directory.)
- **Remote VM sync:** Run `make devenv-sync` to regenerate and rsync configuration across remote targets.
- **Related Docs & ADRs:**
  - `node-manager/README.md` (CLI commands and generation targets)
  - `docs/adr/0002-central-ai-agent-config-symlinks.md` (Central symlink design)
  - `docs/adr/0004-claude-code-conversion.md` (Claude Code conversion & guard hooks)
  - `docs/adr/0005-codex-generated-agents-and-litellm-fallback.md` (Codex generated agents & profiles)
  - `docs/adr/0006-copilot-cli-fallback-and-multi-agent.md` (Copilot conversion)

## Harnesses

| Harness | Agent dir | Command/skill dir | Router fallback wrapper | Notes |
|---|---|---|---|---|
| **opencode** | `~/.config/opencode/agents` | `~/.config/opencode/skills`, `~/.config/opencode/commands` | None (native TUI / router session config) | Primary harness; canonical source in `ai-agent-config/` |
| **claude** | `~/.claude/agents` | `~/.claude/skills` | None | Native Claude Code harness |
| **codex** | `~/.codex/agents` | `~/.codex/prompts` | `codex-fallback` (`~/.local/bin/codex-fallback`) | Use `--profile litellm-agents` for canonical multi-agent patterns; subscription-first fallback remains `litellm-paid` |
| **copilot** | `~/.copilot/agents` (per-file symlinks) | `~/.copilot/skills` (whole-dir symlink) | `copilot-fallback` (`~/.local/bin/copilot-fallback`) | Subscription-first fallback to router model `copilot-fallback`; aliases `copilot`, `copilot-byok`; ACP server for Zed; VS Code BYOK chat provider |
| **cursor** | `~/.cursor/agents` (per-file symlinks) | `~/.cursor/skills` (whole-dir symlink) | None (subscription-first `Auto`) | Generated roles, three explicit command skills, and common fail-closed hooks. Native `readonly` applies per role; writable-child role policy is unavailable because tool hooks do not receive that role identity. |
| **zed** | `agent.profiles` in `~/.config/zed/settings.json` | `~/.agents/skills/` (and `~/.config/zed/skills`) | None (native LiteLLM & Ollama provider blocks) | Native Zed Agent profiles generated via `zed-settings`; slash commands/skills discoverable from `~/.agents/skills/` |

---

## Codex Router Profile

Codex has two distinct LiteLLM paths. They are deliberately separate:

- `litellm-paid` is the quota fallback profile. `codex-fallback` starts with
  subscription-backed Codex and selects this profile only after a recognizable
  quota or rate-limit failure.
- `litellm-agents` is the canonical multi-agent profile. Start a deliberate
  orchestration run with `codex --profile litellm-agents` so every generated
  subagent resolves through the LiteLLM router from its first request.

`make codex-config` creates both profiles in `~/.codex/` after it reconciles
the LiteLLM provider. `make devenv-sync` performs the same merge remotely.
The profile only selects the provider and the primary router model; it contains
no secret. Codex reads `LITELLM_API_KEY` from its inherited environment.

Canonical frontmatter remains the source of truth. A `codex:` block is a
narrow per-harness override for a role whose canonical model is unavailable or
unsuitable in Codex. The current orchestration pins are `coder` on
`gpt-5.6-luna` at medium effort and `critic` on `gpt-5.6-terra` at high effort.
Regenerate the Codex view after changing one of these overrides:

```sh
cd node-manager
make generate-codex-agents
make codex-config LITELLM_HEAD_IP=<router-ip> ARGS="--yes"
```

For Zed ACP, use a separate custom agent backed by the current
`@agentclientprotocol/codex-acp` adapter and the normal local Codex CLI. Set
`CODEX_PATH` to that CLI, `MODEL_PROVIDER=litellm`, and `CODEX_CONFIG` to the
same LiteLLM provider configuration used by `~/.codex/config.toml`. Leave the
key out of Zed settings: the adapter inherits `LITELLM_API_KEY` from Zed's
environment. The older native `codex-acp` adapter is incompatible with the
router's Responses API model-list shape and should not be used for this
profile.

---

## Pattern 1 — Frontier planner/architect

**Goal:** a frontier model plans; cheaper agents execute.

| Agent | Mode | Model | Why |
|---|---|---|---|
| `architect` | primary (+ subagent via `mode: all`) | **user's model selector** (no pin) | The planning brain is whatever frontier model the user selects |

- No `model:` in frontmatter → inherits the session model. Pick `claude-opus-4-8`, `gemini-3.1-pro-preview`, `gpt-5.5-pro`, … in the TUI model selector and the architect uses it.
- Permission fence: can only edit `*.md`/`*.qmd` — it writes plans and reports, never source code.
- Entry point: `/design <feature>` or select `architect` with Tab.
- Plans land in `docs/plans/YYYY-MM-DD-<slug>.md`, formatted so a no-judgment executor can run them: goal, non-goals, design decisions, ordered steps with acceptance criteria, testing requirements, explicit "do not guess" flags.
- Uses the **grilling** skill (Matt Pocock) to interrogate the user one question at a time before planning; **grill-with-docs** additionally maintains `CONTEXT.md` + `docs/adr/` via **domain-modeling**.

## Pattern 2 — Orchestrated build loop

**Goal:** plans get executed by less capable agents, with independent verification at every step and a frontier model as the final safety net.

```
        ┌─────────────────────────────────────────────┐
        │  orchestrator (primary, user's model,       │
        │  coordinates only — never writes code)      │
        └─────────────────────────────────────────────┘
              │ per plan step, max 3 cycles:
              ▼
   coder ──► tester ──► test-reviewer ──FAIL──► back to coder
 (gemini-    (claude-     (deepseek-v4-pro,        │ PASS
  3.5-flash-  haiku-4-5,   re-runs                  ▼
  lite)       runs suite)  everything)        ... next step ...
                                                    │ all steps pass
                                                    ▼
                                              critic (FRONTIER,
                                              pinned gemini-3.1-pro-preview)
                                                    │ APPROVE
                                                    ▼
                                              final report
```

| Agent | Mode | Model | Role |
|---|---|---|---|
| `orchestrator` | primary | user's selector | Delegates everything; briefs are self-contained |
| `coder` | subagent | `litellm/gemini-3.5-flash-lite` | Implements one step per invocation, minimal diffs. **No shell** — verification is the tester's job |
| `tester` | subagent | `opencode/claude-haiku-4-5` | Writes/runs tests under **test-guardian** honesty rules. Shell limited to test-runner + linter/typechecker commands |
| `test-reviewer` | subagent | `litellm/deepseek-v4-pro` | **Judge keeping the tester honest**: independently re-runs the suite, hunts skips/deletions/weakened assertions, verdicts PASS/FAIL. Shell limited to test-runners + linters (check-only) + read-only git. Watch pin — swap up at first rubber-stamp |
| `critic` | subagent | `litellm/gemini-3.1-pro-preview` | Frontier final gate: plan coverage, cross-step consistency, integration risk. Shell limited to read-only git. Gemini 3.x: `thinkingLevel` only — never combine with `thinkingBudget` (Vertex 400s, see Empty subagent returns below) |

Key invariants:
- **Verifier independence** — the agent claiming success and the agent checking it are always different agents *and different model families* (coder=Gemini, tester=DeepSeek, judge=Nemotron).
- **Honesty gates** — `test-guardian` skill: zero failures, no new skips, no deleted/weakened tests, blocked list instead of silent neutralization.
- **Frontier asymmetry** — cheap/local models do the volume work; one frontier pass (`critic`) reviews the complete diff against the original plan.

Entry point: `/deliver <plan-file-or-task>`.

## Tool gating (no-bash policy)

Agents with unrestricted shell access wander outside their purpose. So exploration happens exclusively through built-in tools — `read`, `grep`, `glob`, `webfetch`, plus the `context7` skill for library docs — and `bash` is `deny` for every agent except two narrow, non-wanderable exceptions that have no built-in equivalent:

- **Test-runner allowlist** (`tester`, `test-reviewer`): `npm test*`, `npm run test*`, `npx vitest*`, `npx jest*`, `pnpm test*`, `yarn test*`, `bun test*`, `pytest*`, `go test*`, `cargo test*`, `make*`, plus **R** — `Rscript -e testthat::*` / `Rscript -e devtools::test*` (quoted and bare forms), `R CMD check*` — and **Quarto** — `quarto render*`, `quarto check*`. Both agents must execute the suite (the reviewer independently). R code is tested with testthat; `.qmd` report code is verified by rendering. If a project's runner isn't listed, agents are instructed to report the exact command needed rather than work around it; extend the list via a project-level `.opencode/agents/tester.md` override.
- **Linter/typechecker allowlist** (`tester`, `test-reviewer`): `ruff*`, `black*`, `pyright*`, `mypy*` (bare + `uv run`/`uvx`/`poetry run` forms), `npx eslint*`/`prettier*`/`tsc*`/`biome*`, `golangci-lint*`/`go vet*`, `Rscript -e lintr::*` (quoted/bare), `air*`. Rationale: the user sees these diagnostics in their editor via LSP, so the loop must verify them before claiming green. The reviewer is instructed to use check-only modes (never `--fix`/`--write`) to preserve its read-only role.
- **Read-only git** (`test-reviewer`, `critic`): `git status*`/`git diff*`/`git show*`/`git log*` — diff inspection is their core input.
- **Ticket tracker CLIs** (`architect`, `orchestrator`): `gh issue*`/`glab issue*` are **ask** for writes (create/edit/close/comment), allow for reads (auth status/list/view) plus `git remote get-url*` — tickets are published to the upstream repo manager for hygiene, never silently.
- **Worktree setup** (`orchestrator`): `git worktree*` is **ask** — one worktree per parallel pipeline, with user consent.

Additional guardrails:

- `task: deny` on every agent except `orchestrator` and `committee` — no delegation chains that escape the loop's structure.
- `edit: deny` **and** `write: deny` on every agent except `coder`, `tester`, and (markdown-only) `architect` — `write` (create files) is a separate tool from `edit` with its own permission ruleset; fences must mirror both or read-only agents can still create files. The orchestrator's sole write is `.scratch/**/*.md` (the loop journal).
- Consequence for the loop: the coder cannot run builds/tests at all. That is deliberate separation of duties — it writes code, the tester proves it.

## Exploration policy (built-in `explore`, explicit delegation)

Exploration is ALWAYS delegated to the built-in **`explore`** subagent — never done ad hoc by reasoning agents, who would burn their context windows on raw grep/read output. The rules, stated explicitly in every agent prompt:

- **Delegating agents** (`architect`, `orchestrator`, `committee`): any question broader than a known file goes to `explore` with a concrete question and a thoroughness level (`quick`/`medium`/`very thorough`). They read files directly only when they know exactly which file they need.
- **Non-delegating agents** (everyone else; they have `task: deny`): work from the brief plus the specific files it cites. If context is missing, they report `missing context: …` — the orchestrator runs `explore` on the gap and re-briefs. Wandering the codebase is explicitly out of role.
- `explore` itself is pinned to a tiny/free model (`google-vertex/gemini-flash-lite-latest`, via the `agent.explore` override in `opencode.jsonc`) so context management is cheap; swap the pin to taste. Note: the pin carries a `reasoning: false` provider override — models.dev marks flash-lite as reasoning-capable, which makes opencode inject `thinkingConfig.includeThoughts`, and Vertex 400s on it (see Empty subagent returns below). If you swap the pin to another thinking-flagged lite model and explore starts returning empty, check for the same interaction.
- **Library/framework docs**: the `context7` MCP server (`opencode.jsonc` → `mcp`) gives every agent — including no-bash loop agents — doc lookup without shell access. The curl-based `context7` skill remains for bash-enabled agents.

## LSP and linters

- **LSP servers** are enabled globally (`lsp` object in `opencode.jsonc` — the object form keeps built-ins on while adding `ark` for R/Quarto). Built-ins include `pyright` for Python (requires `pyright` installed in the project), `typescript`, `eslint`, `gopls`, etc. Diagnostics flow back automatically on file open/edit.
- **The `lsp` tool** (agent-callable diagnostics/hover/symbols/references) is experimental: it only exists when opencode is launched with `OPENCODE_EXPERIMENTAL_LSP_TOOL=true` (export it in your shell profile). `tester` and `test-reviewer` carry an explicit `lsp: allow` so the no-ask audit stays complete; the permission defaults to allow for everyone else.
- **CLI linters** are the reliable channel that works without the env flag — hence the linter allowlist on `tester`/`test-reviewer` (see Tool gating). Both channels surface the same underlying diagnostics (pyright the LSP server = pyright the CLI).

## Pattern 3 — All-local committee

**Goal:** better answers to hard questions via diverse perspectives, blind judging, and decisive synthesis — zero cloud cost.

```
 committee (primary, coordinates; grounds briefs via built-in explore)
     │ one message, 3 parallel task calls:
     ├──► panelist-qwen35  (Qwen3.6-35B)  — the Pragmatist
     ├──► panelist-qwen27  (Qwen3.6-27B)  — the Skeptic
     └──► panelist-gemma   (Gemma-4-26B)  — the Minimalist
              │ answers anonymized A/B/C, order shuffled
              ▼
         judge (Ornith-1.0-35B, temp 0)     ← run twice with adversarial
              │ scores + winner + best elements    framing for high stakes
              ▼
      synthesizer (deepseek-v4-pro, cloud — diversity from the panel)
              │ decides, doesn't average; preserves real dissent
              ▼
        final answer + per-panelist contributions + residual risks
```

| Agent | Model pin | Lens |
|---|---|---|
| `panelist-qwen35` | `litellm/Qwen3.6-35B-A3B-MTP-GGUF-UD-Q4_K_XL:latest` | Pragmatist — tradeoffs, operability |
| `panelist-qwen27` | `litellm/Qwen3.6-27B-MTP-GGUF:latest` | Skeptic — failure modes, hidden assumptions |
| `panelist-gemma` | `litellm/Gemma-4-26B-A4B-it-GGUF:latest` | Minimalist — simplest sufficient answer |
| `judge` | `litellm/Ornith-1.0-35B-GGUF-UD-Q4_K_XL:latest` | Blind scorer — grades arguments, not identities |
| `synthesizer` | `litellm/deepseek-v4-pro` | Decider — merges best elements, keeps dissent |

Key invariants:
- **Diversity on two axes** — different model families *and* different lenses per panelist.
- **Synthesizer diversity** — the synthesizer intentionally pins a cloud-tier model (not a local GGUF like the panel/judge) so it is never the same model family as any panelist or the judge it adjudicates; it previously shared its exact pin with `panelist-qwen35`, which undermined independent judging. This is a deliberate exception to Pattern 3's otherwise-zero-cloud-cost goal.
- **Blind judging** — the judge never learns which model/lens wrote which answer.
- **One or more judges** — two modes, following Karpathy's `llm-council`:
  - *Ordinary*: one `judge` pass, full scored report.
  - *High-stakes*: **peer ranking** — each panelist re-invoked with the anonymized answers (lenses off) plus the `judge`, all emitting strict `FINAL RANKING:` verdicts the chair aggregates by average position; optional adversarial pass on the aggregate leader.
- The synthesizer weighs answers, judging signals, and agreement/disagreement patterns together — deciding, not averaging; dissent is preserved.
- To run fully local end-to-end, also pick a local model in the selector for the `committee` chair itself.

Entry point: `/committee <question>`.

---

## Skills inventory

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

The awesome-copilot originals were framework-specific (React 19 migration; security-only review); the installed versions keep the portable core — targeted review plans, prioritized evidence-based findings, honesty gates, completion gates, blocked-list escalation — and drop the framework specifics.

## Loop mechanics worth knowing

- **Loop journal**: the orchestrator keeps canonical run state in `.scratch/<feature>/LOOP.md` (its only write permission) — every subagent report becomes a compact record, dispatches are briefed from plan + journal rather than conversational memory, and the final report is journal-driven. Compaction can no longer lose the run.
- **Red-first (TDD) ordering**: default loop is coder → tester → test-reviewer; for logic-heavy steps and bug fixes the orchestrator runs tester FIRST (failing tests from the brief), then coder implements to green.
- **Debug pass**: a step that FAILs twice triggers a `systematic-debugging` pass — root-cause statement required before any third patch.
- **Parallel isolation**: independent steps may run in parallel, but only with one git worktree per pipeline (`using-git-worktrees`); the orchestrator asks consent for `git worktree add` — decline and it runs sequentially.
- **Review protocol**: critic/reviewer findings flow back to the coder under `receiving-code-review` — verify, fix, or reasoned pushback; no performative agreement.
- **Git Commit Ownership ("Coders draft, Reviewers audit, Orchestrators commit")**: Subagents (`coder`, `tester`, `test-reviewer`, `critic`) never stage or commit code to git. Only the Orchestrator creates git commits, and strictly AFTER an independent verification gate (`test-reviewer` or `critic`) confirms a **PASS** or **APPROVE** verdict for a step.

### Unattended subagent anti-hang rules

Subagents run without a human at the keyboard. Any permission that resolves to `ask` will hang them indefinitely — there is nobody to answer the prompt. Every subagent in the loop MUST explicitly resolve all `ask`-defaulting permissions to `allow` or `deny`:

| Permission | Default | Subagent setting | Why |
|---|---|---|---|
| `doom_loop` | `ask` | `deny` | break tight loops instead of hanging |
| `external_directory` | `ask` | `deny` | subagents stay in-project |
| `question` | `allow` | `deny` | subagents can't interact with the user |
| `read` (`.env`) | `ask` | `deny` | fail fast on secrets, don't hang |

**Principle: no subagent permission should ever resolve to `ask`.** When adding a new subagent or a new permission key, audit for `ask` defaults. The orchestrator (primary, interactive) may keep `ask` rules — it has a human.

### Empty subagent returns

Symptom: the orchestrator dispatches to a subagent and the task result comes back **empty — no error, no commentary**. Historically worst for `explore` and `critic`.

Mechanism: the `task` tool's result is the subagent's **final text message only** (`parts.findLast(type === "text")?.text ?? ""`). If the subagent's model stream errors before any text is produced — provider 4xx/5xx, rate limit, timeout, abort — the session ends with no text part and the orchestrator receives `""`. The error exists only in the opencode log (`~/.local/share/opencode/log/opencode.log`, grep `agent=<name>`); it is NOT propagated into the task result.

Root causes found in our logs (2026-08-11/12) and their fixes:

| Cause | Evidence | Fix |
|---|---|---|
| `critic` frontmatter set `thinkingBudget` + `thinkingLevel` together — Vertex rejects the combination with HTTP 400, every call | 18/18 critic stream errors | `critic.md` now sets `thinkingLevel: high` alone (Gemini 3.x knob; budget is 2.5-era) |
| `explore` pin `gemini-flash-lite-latest` is marked `reasoning: true` in models.dev → opencode auto-injects `thinkingConfig.includeThoughts` → Vertex 400 (flash-lite has thinking disabled) | 12 explore stream errors | `provider.google-vertex.models."gemini-flash-lite-latest".reasoning: false` in `opencode.jsonc` |
| Free-tier litellm pins rate-limited / upstream 502s (nemotron via Nvidia, deepseek free) | 55+39 stream errors | current pins moved off saturated free tiers; treat recurrence as infra, swap the pin |
| `chunkTimeout: 60000` aborted litellm streams silent >60s — hostile to thinking models | `error=Aborted` log entries | raised to 300000 (5 min) in `opencode.jsonc` |

Durable mitigations (any future cause):

1. **Plugin: `task-error-surface.ts`** (in this directory's `plugins/`, also shipped in devenv's `.opencode/plugins/` and `skeleton/.opencode/plugins/`). A `tool.execute.after` hook on `task`: when the task result has no text, it reads the child session via the SDK (`output.metadata.sessionId`), finds the assistant message carrying `error`, and rewrites the result to include `<task_error>` with the provider's message (e.g. the HTTP 400 from a bad model name). Verified in v1.18.16: provider errors are caught by `processor.ts` `halt()` and stored on the assistant message, so `prompt()` returns normally and `task.ts` falls back to `""` — the hook closes that gap without forking opencode. Background (`state="running"`) and already-errored tasks are left alone; kill switch `OPENCODE_TASK_ERROR_SURFACE=off`. With the plugin active, most rows in the table above surface in-band instead of as silence.
2. The orchestrator's explicit **empty-dispatch protocol** remains as the backstop (an error can still leave no trace, e.g. an abort before the error part is written): an empty task result is a FAILED dispatch — retry once demanding a plain-text final report, then journal + surface to the user. Silence never advances the loop.

Related latent fix: `subagent_depth: 2` in `opencode.jsonc` — the default of 1 blocked `test-reviewer` → `explore` despite its `task: {explore: allow}` permission (the reviewer would get "Subagent depth limit reached" mid-audit). Delegation chains remain fenced by each agent's own `task` permission.

**Which fixes propagate to devenv remotes:** the agent-file fixes (critic `thinkingLevel`, orchestrator empty-dispatch protocol, linter allowlists) ship via this directory's symlinks. The config-level fixes ship through `node-manager`'s cloud-configurator: `build_remote_opencode_candidate()` carries `subagent_depth: 2`, `chunkTimeout: 300000` / `timeout: 900000`, the ark LSP block, and the context7 MCP server, deep-merged into each remote's opencode config by `make devenv-sync`. Local-only by design: the `gemini-flash-lite-latest` `reasoning: false` override and the google-vertex pins (remotes have no vertex provider; their agent pins come from `node-manager/apps/preferences.yaml`).

Router-side counterparts (LiteLLM at 10.49.20.68:4000): the same failure class exists one layer down. `stream_timeout` is the max wait for the first/inter-chunk window of a stream — if it's tighter than a thinking model's silent-thinking window, the router kills the stream and opencode sees the same empty result (keep it ≥ opencode's `chunkTimeout`, currently 300s). `num_retries`/`allowed_fails` cover 429/5xx/connection errors only — never deterministic 400s (bad params) and never empty-but-200 responses. `allowed_openai_params` is additive-permissive (it force-passes listed params to `openai/*` backends); `["reasoning_effort"]` is the right entry — it's the provider-agnostic thinking knob LiteLLM translates per-provider, and it's what opencode sends for reasoning variants.

## Test fixtures

`errprobe` is not a production agent — it is pinned to a deliberately
nonexistent model (`litellm/nonexistent-model-zzz`) so every call fails at
the LiteLLM router, exercising the `task-error-surface.ts` plugin's
error-detection path end-to-end. It is intentionally excluded from the
orchestrator's `task` allowlist (it should never fire in a real run) and is
invoked out-of-band only (Tab-select, `opencode run --agent errprobe`, or a
dedicated `/errprobe-check` command if one is added later). Counted
separately from the 12 production agents in the tree diagram above.

## Changing models

Every pin is one `model:` line in the agent's frontmatter. Format: `provider/model-id` exactly as listed by `opencode models`.

- **Architect / orchestrator / committee chair**: unpinned by design → the TUI model selector controls them.
- **Critic**: any frontier id, e.g. `litellm/gemini-3.1-pro-preview`, `litellm/gpt-5.5-pro`.
- **Local roster**: `litellm/Qwen3-Coder-30B-A3B-Instruct-GGUF:latest`, `litellm/Qwen3.6-35B-A3B-MTP-GGUF-UD-Q4_K_XL:latest`, `litellm/Qwen3.6-27B-MTP-GGUF:latest`, `litellm/Gemma-4-26B-A4B-it-GGUF:latest`, `litellm/Ornith-1.0-35B-GGUF-UD-Q4_K_XL:latest`, `lemonade/gemma-4-26B-A4B-it-GGUF-Q4_K_M`.
- **More judges/panelists**: copy the agent file, change the `model:` pin and description, and name it in the orchestrator/committee prompt.
- Note: `plugins/router-affinity.ts` pins each opencode session (each subagent gets its own) to one LiteLLM backend for KV-cache reuse; parallel panelists spread across the cluster automatically.
- **On devenv remotes**: harness-level agent pins (explore, title, summary, …) come from `node-manager/apps/preferences.yaml` (`opencode.agents`), not from a hand-edited `opencode.jsonc` — edit there and re-run the sync rather than drifting the remote's config file.

## Project-level overrides

Anything here can be overridden per project: drop a same-named file in the project's `.opencode/agents/` (or redefine in project `opencode.json`) — project config wins over global. Useful for pinning the coder to a project-appropriate model or adjusting the loop limits.

## Portability (pi, hermes, Claude Code, …)

The definitions are plain markdown + YAML frontmatter with no opencode-only tooling in the bodies:

- **Bodies** (the prompts) are harness-agnostic — copy as-is.
- **Frontmatter mapping**: `description` is universal; `mode: subagent` ≈ Claude Code `subagent` semantics / pi task agents; `model` is a provider-prefixed id here, map to the target harness's model syntax; `permission` maps to each harness's tool gating (or drop it and rely on the body's discipline rules).
- **Skills** use the standard `SKILL.md` shape that Claude Code, pi, and hermes all scan; `~/.agents/skills/` is already auto-loaded by opencode as well, so skills can live there for true cross-harness sharing.
- **Orchestration** is encoded in the orchestrator/committee agent prompts as plain instructions over a generic "spawn subagent" capability — any harness with subagents can execute the same loops.

## Attributions

- `grilling`, `grill-me`, `grill-with-docs`, `domain-modeling`, `codebase-design`, `to-spec`, `to-tickets` — [mattpocock/skills](https://github.com/mattpocock/skills), MIT License.
- `verification-before-completion`, `systematic-debugging`, `receiving-code-review`, `test-driven-development`, `using-git-worktrees`, `dispatching-parallel-agents` — [obra/superpowers](https://github.com/obra/superpowers), MIT License. (Its `docs/porting-to-a-new-harness.md` is also the best reference when porting these patterns to pi/hermes.) `requesting-code-review` was reviewed and removed — redundant with the loop's fixed critic/test-reviewer roles; its useful logic (severity triage, deviation-intent checks, praise-first calibration) was folded into `critic.md` and `orchestrator.md`.
- `code-review`, `test-guardian` — adapted from [github/awesome-copilot](https://github.com/github/awesome-copilot) agent definitions, MIT License.
- Committee peer-ranking mode and the `FINAL RANKING` verdict format — patterns from [karpathy/llm-council](https://github.com/karpathy/llm-council) (provided "as is for inspiration", no license file; concepts re-implemented here, no code copied).
