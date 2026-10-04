"""This proposal was blocked. It is a trial and it counts toward N.

reason:  static_check_unrepaired
detail:  2 repair attempts, last: src/strategies/_proposal_17496_170b57000.py:86: error: Unsupported left operand type for - ("None")  [operator]; src/strategies/_proposal_17496_170b57000.py:102: error: Unsupported left operand type for + ("None")  [operator]

No strategy was produced, so there is nothing to run. The file raises at module
level on purpose: the run then fails to load, `run.py` records `outcome:
crashed` with this message, and the trial keeps its row in the ledger. The
alternative -- returning nothing and skipping the trial -- would shrink N by
exactly the proposals that failed, which is the cheapest way to cheat a
multiple-testing correction and the one nobody would look for.
"""

raise RuntimeError("agent_blocked: static_check_unrepaired")
