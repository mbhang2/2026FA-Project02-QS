"""This proposal was blocked. It is a trial and it counts toward N.

reason:  call_failed
detail:  HTTP 422: {"error":{"message":"Budget has been exceeded! Key=fadb18d935dfd1842b09e4f37f6814d6216000115cabe81dbc57273b52a77abd Current cost: 200.31537968, Max budget: 200.0","type":"budget_exceeded","param":null

No strategy was produced, so there is nothing to run. The file raises at module
level on purpose: the run then fails to load, `run.py` records `outcome:
crashed` with this message, and the trial keeps its row in the ledger. The
alternative -- returning nothing and skipping the trial -- would shrink N by
exactly the proposals that failed, which is the cheapest way to cheat a
multiple-testing correction and the one nobody would look for.
"""

raise RuntimeError("agent_blocked: call_failed")
