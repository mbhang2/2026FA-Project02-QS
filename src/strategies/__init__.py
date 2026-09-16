"""The only tree the research agent may write.

Enforced two ways: tool permissions in the agent config, and a CI check comparing
changed paths against an allowlist. The CI check is the one that actually holds --
configs drift and get copied wrong.
"""
