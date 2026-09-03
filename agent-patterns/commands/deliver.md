---
description: Execute a plan or task with the full build loop — coder, tester, test-reviewer per step, then the frontier critic as the final gate.
agent: orchestrator
---

Deliver this with the full build loop: $ARGUMENTS

If $ARGUMENTS is a path to a plan file, read and execute it step by step. If it is a bare task and anything is ambiguous, grill me first. Per step run coder → tester → test-reviewer (max 3 cycles), then finish with the critic on the complete diff, and report.
