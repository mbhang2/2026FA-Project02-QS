"""This proposal was blocked. It is a trial and it counts toward N.

reason:  static_check_unrepaired
detail:  2 repair attempts, last: src/strategies/_proposal_34897_20049f240.py:69: error: "object" has no attribute "add_bar"  [attr-defined]; src/strategies/_proposal_34897_20049f240.py:107: error: "object" has no attribute "reset"  [attr-defined]; src/strategies/_proposal_34897_20049f240.py:121: error: "object" has no attribute "is_empty"  [attr-defined]; src/strategies/_proposal_34897_20049f240.py:123: error: "object" has no attribute "value_area"  [attr-defined]; src/strategies/_proposal_34897_20049f240.py:128: error: "object" has no attribute "distance_from_vpoc"  [attr-defined]; src/strategies/_proposal_34897_20049f240.py:131: error: "object" has no attribute "value_area_position"  [attr-defined]

No strategy was produced, so there is nothing to run. The file raises at module
level on purpose: the run then fails to load, `run.py` records `outcome:
crashed` with this message, and the trial keeps its row in the ledger. The
alternative -- returning nothing and skipping the trial -- would shrink N by
exactly the proposals that failed, which is the cheapest way to cheat a
multiple-testing correction and the one nobody would look for.
"""

raise RuntimeError("agent_blocked: static_check_unrepaired")
