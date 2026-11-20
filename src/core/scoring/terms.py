"""The scoring terms for a profile: what a session return is divided by, what an
idle session is charged, and what funding is paid. One resolver, every caller.

**Why one function.** `run.py` scores a trial; `scripts/l2_gate.py`,
`scripts/holdout.py` and `scripts/nq_campaign.py` rebuild that trial's null later.
If any of them computed the terms on its own, the observed score and its null
would be built from different return vectors and the p-value would compare
nothing with nothing -- the defect `round_turn.identity_primary` exists to catch.
Before crypto, those scripts relied on the module defaults matching the config
(`IDLE_CHARGE` = `campaign.idle_charge` = 0.18), which held by coincidence.

Two units, chosen per profile with `scoring_units`:

* `points` (the default; every NQ profile). `notional_base` is the profile's
  constant (1.0, so returns are index points) and the idle charge is
  `campaign.idle_charge` points per contract. Unchanged to the bit.
* `notional` (crypto; pre-registration Amendment 16). Each session's return is
  divided by that session's opening price, so it is a share of one lot-coin's
  notional and means the same in 2023 and 2026. The idle charge is
  `idle_charge_fraction` of that notional, and with `funding: true` the position
  held at each funding settlement pays `position x mark x rate`.
"""

from __future__ import annotations

from typing import Any, Sequence

from .metrics import IDLE_CHARGE, SessionSpan


def scoring_terms(cfg: dict, profile: dict, dataset: Any,
                  calendar: Sequence[SessionSpan]) -> dict:
    """`{notional_base, idle_charge, funding}`, ready to pass as keyword arguments
    to `score`, `score_diagnostics`, `session_returns` and the round-turn null."""
    units = profile.get("scoring_units", "points")
    if units == "points":
        return {
            "notional_base": profile.get("notional_base", 1.0),
            "idle_charge": cfg.get("campaign", {}).get("idle_charge", IDLE_CHARGE),
            "funding": None,
        }
    if units == "notional":
        if "notional_base" in profile:
            raise ValueError("scoring_units: notional derives the notional per session; "
                             "a constant notional_base would be ignored, so it is refused")
        sessions = [c.session for c in calendar]
        opens = dataset.session_open_prices(sessions)
        frac = float(profile["idle_charge_fraction"])
        return {
            "notional_base": opens,
            "idle_charge": {s: frac * opens[s] for s in sessions},
            "funding": dataset.funding_series(sessions) if profile.get("funding") else None,
        }
    raise ValueError(f"unknown scoring_units {units!r}; expected 'points' or 'notional'")
