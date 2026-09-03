---
name: committee
description: Convene the three local panelists in parallel, blind judge, and synthesize a decisive answer with rationale. Invoke explicitly with /committee.
disable-model-invocation: true
---

<!-- Skill equivalent of ai-agent-config/commands/committee.md. -->

Convene the committee on the question or topic given in the invoking message.

Dispatch the three panelists in parallel (panelist-gemma, panelist-qwen27, panelist-qwen35), pass their anonymized answers to the judge, and have the synthesizer merge them into one final decisive answer with rationale.
