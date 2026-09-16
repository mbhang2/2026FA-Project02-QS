---
name: code-review
description: Structured, context-targeted code review with prioritized findings and a written report. Use when reviewing a diff, PR, or change set for correctness, security, reliability, performance, or maintainability — especially as the review/judge step in a multi-agent loop.
---

<!-- Generalized from github/awesome-copilot `agents/se-security-reviewer.agent.md` — MIT License, (c) Microsoft/GitHub. Broadened from security-only to general review; framework-agnostic. -->

# Code Review

Produce a targeted, evidence-based review. Never a generic checklist dump — select the review categories that fit THIS change.

## Step 0: Create a targeted review plan

Analyze what you are reviewing before reviewing anything:

1. **Change type?**
   - New feature → correctness of logic, edge cases, API contract
   - Bug fix → root cause truly fixed, regression risk, test coverage of the bug
   - Refactor → behavior preservation, dead code, missed call sites
   - Config/infra → blast radius, secrets, rollback path

2. **Risk level?**
   - High: auth, payments, data mutation, concurrency, external I/O, security boundaries
   - Medium: user-facing APIs, state management, error handling
   - Low: UI cosmetics, utilities, docs

3. **Pick 3–5 review categories** from: correctness, security, reliability, performance, maintainability, test quality, and taste/idiom (architectural drift, NIH syndrome, wrong-layer placement).

State the chosen categories in one line before reviewing.

## Step 1: Review with evidence

For every finding, cite the exact file and line, and show the problematic code. Rate each finding:

- ⛔ **P1 — Must fix**: will cause incorrect behavior, data loss, security holes, or production failures
- ⚠️ **P2 — Should fix**: latent bugs, missing error handling, weak tests on changed paths
- 💡 **P3 — Consider**: style, simplification, future-proofing

Verify before accusing. If you suspect a bug, trace the actual call paths. If you claim a test is missing, check first. A wrong finding costs more than a missed one.

## Step 2: Adversarial self-check

Re-read your P1 findings. For each, ask: "Is there any reasonable interpretation where this is fine?" Downgrade or drop findings that don't survive.

## Output format

```markdown
# Code Review: [change/description]
**Verdict**: [Approve / Approve with changes / Request changes]
**Reviewed categories**: [the 3–5 from Step 0]

## ⛔ P1 — Must fix
- `path/file.ts:42` — [issue] — [concrete fix]

## ⚠️ P2 — Should fix
- ...

## 💡 P3 — Consider
- ...

## What was done well
- [1–3 specific positives — reviewers who only complain lose credibility]
```

Keep the report tight. Five verified findings beat twenty speculative ones.
