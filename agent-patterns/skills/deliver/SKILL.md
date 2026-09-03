---
name: deliver
description: Execute a plan or task with the full build loop — coder, tester, test-reviewer per step, then the frontier critic as the final gate. Invoke explicitly with /deliver.
disable-model-invocation: true
---

<!-- Skill equivalent of ai-agent-config/commands/deliver.md. -->

Deliver the task or plan given in the invoking message with the full build loop.

If the invoking message names a plan file path, read and execute it step by step. If it is a bare task and anything is ambiguous, grill me first. Per step run coder → tester → test-reviewer (max 3 cycles), then finish with the critic on the complete diff, and report.
