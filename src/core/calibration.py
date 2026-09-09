"""Two-sided calibration: the null half and the power half.

A gate that rejects everything passes a null test trivially and is useless, so
calibration is two-sided. Both halves are ordinary runs on the synthetic dataset,
which is the test that the `Dataset` abstraction is real rather than decorative.

The accept rule here is a **placeholder standing in for the committee**. It is
deliberately the dumb single-scalar rule the loop starts with, so the numbers it
produces are a floor: whatever the committee does later should be no worse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from .datasets.synthetic import SyntheticDataset, SyntheticSpec
from .engine import Engine
from .metrics import placeholder_score
from .scorer import score, score_diagnostics
from .types import ScoreTuple, Split


@dataclass(frozen=True)
class AcceptRule:
    """MVP stand-in for the committee. `t_min` is the only real dial.

    Carried over from Task 3 unchanged at 3.0. What changed is the statistic it
    thresholds -- `ScoreTuple.significance`, the ratio times sqrt(N) -- not the
    threshold. Moving the threshold to make a curve look monotone would hide a
    broken metric rather than fix one.
    """

    t_min: float = 3.0
    require_positive_net: bool = True

    def accepts(self, s: ScoreTuple) -> bool:
        if s.rejected:
            return False
        if self.require_positive_net and s.net_pnl <= 0:
            return False
        return s.significance >= self.t_min


@dataclass(frozen=True)
class TrialResult:
    seed: int
    signal_strength: float
    score: ScoreTuple
    accepted: bool
    fill_model: str
    diagnostics: object | None = None
    # The metric Task 3 shipped, computed on the *same* run so the two
    # acceptance curves differ only in the metric and not in the data.
    placeholder_t: float = 0.0
    placeholder_trades: int = 0
    accepted_placeholder: bool = False


def run_trial(
    strategy_factory: Callable[[], object],
    *,
    signal_strength: float,
    seed: int,
    n_sessions: int = 200,
    ticks_per_session: int = 780,
    vol_per_tick: float = 0.15,
    fill_model: str = "unfavorable",
    split: Split = Split.VALIDATION,
    warmup_sessions: int = 1,
    rule: AcceptRule | None = None,
) -> TrialResult:
    ds = SyntheticDataset(
        SyntheticSpec(
            n_sessions=n_sessions,
            ticks_per_session=ticks_per_session,
            vol_per_tick=vol_per_tick,
            signal_strength=signal_strength,
            seed=seed,
        )
    )
    eng = Engine(
        ds,
        strategy_factory(),
        split=split,
        fill_model=fill_model,
        warmup_sessions=warmup_sessions,
        seed=seed,
    )
    eng.run()
    period = eng.session_calendar()
    s = score(eng.blotter, period=period)
    diag = score_diagnostics(eng.blotter, period)
    eng.record.score = s.as_dict()
    eng.record.diagnostics = diag.as_dict()
    rule = rule or AcceptRule()
    pt, pn, pnet = placeholder_score(eng.blotter)
    return TrialResult(
        seed=seed,
        signal_strength=signal_strength,
        score=s,
        accepted=rule.accepts(s),
        fill_model=fill_model,
        diagnostics=diag,
        placeholder_t=pt,
        placeholder_trades=pn,
        accepted_placeholder=(pt >= rule.t_min and pnet > 0),
    )


def acceptance_rate(
    strategy_factory: Callable[[], object],
    *,
    signal_strength: float,
    seeds: Sequence[int],
    rule: AcceptRule | None = None,
    **kw,
) -> tuple[float, list[TrialResult]]:
    """Fraction of independent trials the rule accepts.

    At `signal_strength=0` this is a false-acceptance rate on a known null, which
    is a publishable number. Above zero it is power.
    """
    rule = rule or AcceptRule()
    out = [
        run_trial(
            strategy_factory,
            signal_strength=signal_strength,
            seed=seed,
            rule=rule,
            **kw,
        )
        for seed in seeds
    ]
    return sum(r.accepted for r in out) / len(out), out
