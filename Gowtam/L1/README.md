# L1 Orchestrator — Immutable MVP Integration

This folder (`Gowtam/L1/`) contains **only** the core agentic orchestration logic (the build loop) isolated from all speculative execution engines.

It implements the isolated `planner -> worker <-> reviewer` multi-session loop that turns an `ApprovedCandidate` (hypothesis) into a single implemented Python file, ready for execution. 

## What is in here

- `loop/`: The orchestration driver and the step definitions.
- `schemas/`: The frozen Pydantic contracts for inputs and outputs.
- `opencode_bridge/`: The invocation and parsing layer for shelling out to `opencode`.
- `.opencode/agent/`: The prompt permission limits for the three core roles.

## How it integrates with the Immutable Simulator

1. **Write Path Whitelist**: The worker writes to `current.py` instead of `train.py`.
2. **Context APIs**: The `prompt_doc.py` injects `context-api.md` and `simulator-interface.md` into the agent's prompts. 
3. **Execution & Committee**: The loop in `driver.py` finishes at `ENGINE_RUN`. At this point, the external harness should invoke `python run.py` (the immutable engine) and then hand off the `trial_log` to the committee (Statistician, Auditor, Judge) which are built and maintained by the teammate.

This folder is self-contained and ready to be plugged into the main project.
