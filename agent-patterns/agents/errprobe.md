---
name: errprobe
description: Error-surface probe — pinned to nonexistent-model-zzz so every call fails at the LiteLLM router; used to test that subagent LLM errors surface to the orchestrator (task-error-surface plugin)
mode: subagent
model: litellm/nonexistent-model-zzz
temperature: 0
# claude.skip: no meaningful Claude Code port — this fixture exists solely to
# fault-inject against opencode's task-error-surface.ts plugin.
claude:
  skip: true
codex:
  skip: true
copilot:
  skip: true
permission:
  edit: deny
  write: deny
  bash: deny
  task: deny
  doom_loop: deny
  question: deny
  external_directory: deny
  read:
    "*.env": deny
    "*.env.*": deny
    "*.env.example": allow
---

You are a test probe. Reply with the single word: pong
