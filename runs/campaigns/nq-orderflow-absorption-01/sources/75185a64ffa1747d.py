"""This proposal was blocked. It is a trial and it counts toward N.

reason:  static_check_unrepaired
detail:  2 repair attempts, last: src/strategies/_proposal_54540_172d13000.py:51: error: Need type annotation for "_session_bars" (hint: "_session_bars: list[<type>] = ...")  [var-annotated]; src/strategies/_proposal_54540_172d13000.py:52: error: Need type annotation for "_session_volume_profile"  [var-annotated]; src/strategies/_proposal_54540_172d13000.py:55: error: Need type annotation for "_recent_bars" (hint: "_recent_bars: list[<type>] = ...")  [var-annotated]; src/strategies/_proposal_54540_172d13000.py:56: error: Need type annotation for "_recent_efficiencies" (hint: "_recent_efficiencies: list[<type>] = ...")  [var-annotated]; src/strategies/_proposal_54540_172d13000.py:147: error: Incompatible types in assignment (expression has type "str | None", variable has type "None")  [assignment]

No strategy was produced, so there is nothing to run. The file raises at module
level on purpose: the run then fails to load, `run.py` records `outcome:
crashed` with this message, and the trial keeps its row in the ledger. The
alternative -- returning nothing and skipping the trial -- would shrink N by
exactly the proposals that failed, which is the cheapest way to cheat a
multiple-testing correction and the one nobody would look for.
"""

raise RuntimeError("agent_blocked: static_check_unrepaired")
