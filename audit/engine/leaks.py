"""Leak / look-ahead heuristics. Validator-owned; never used to 'fix' a Sharpe."""

from __future__ import annotations

import math
from typing import Any


def pearson(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((xs[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ys[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def contemporaneous_corr(signal: list[float], returns: list[float]) -> float:
    """corr(signal[t], return[t]). High values with delay_bars=0 are look-ahead."""
    return pearson(signal, returns)


def next_bar_corr(signal: list[float], returns: list[float]) -> float:
    if len(signal) < 3 or len(returns) < 3:
        return 0.0
    return pearson(signal[:-1], returns[1:])


def audit(
    signal: list[float],
    returns: list[float],
    *,
    delay_bars: int,
    kind: str | None = None,
) -> dict[str, Any]:
    """Return a machine-readable leak audit.

    FAIL when a zero-delay experiment's signal is highly correlated with the
    same-bar return (the classic 'traded on today's close using today's close
    return' leak), unless the experiment is an explicit sanity check of that
    failure mode.
    """
    same = contemporaneous_corr(signal, returns)
    nxt = next_bar_corr(signal, returns)
    findings: list[str] = []
    verdict = "PASS"
    if delay_bars < 0:
        verdict = "FAIL"
        findings.append("delay_bars < 0 is invalid")
    if delay_bars == 0 and abs(same) >= 0.5 and kind != "sanity":
        verdict = "FAIL"
        findings.append(
            f"look-ahead: corr(signal, same-bar return)={same:.3f} with delay_bars=0"
        )
    if delay_bars == 0 and kind == "replicate":
        findings.append(
            "replicate experiments may use delay_bars=0 only to match a paper; "
            "do not treat as realistic tradability"
        )
    return {
        "verdict": verdict,
        "delay_bars": delay_bars,
        "corr_same_bar": same,
        "corr_next_bar": nxt,
        "findings": findings,
    }


def point_in_time_ok(
    signal_fn,
    closes: list[float],
    *,
    bump: float = 1.5,
    max_probes: int = 64,
) -> dict[str, Any]:
    """True when signal[t] is invariant to closes[t+1:].

    Full scan when the series is short (daily synthetic). Long 1-minute
    tapes probe a grid so the check stays O(probes * n) instead of O(n^2).
    """
    base = signal_fn(list(closes))
    if len(base) != len(closes):
        return {"ok": False, "index": None, "findings": ["signal length != closes"]}
    n = len(closes)
    if n < 2:
        return {"ok": True, "index": None, "findings": []}
    if n - 1 <= max_probes:
        indices = list(range(n - 1))
    else:
        step = max(1, (n - 1) // max_probes)
        indices = list(range(0, n - 1, step))
        if n - 2 not in indices:
            indices.append(n - 2)
    for t in indices:
        mutated = list(closes)
        mutated[t + 1] = mutated[t + 1] * bump
        if mutated[t + 1] == closes[t + 1]:
            mutated[t + 1] = closes[t + 1] + 1.0
        sig = signal_fn(mutated)
        if sig[t] != base[t]:
            return {
                "ok": False,
                "index": t,
                "findings": [f"signal[{t}] changed when closes[{t + 1}] was mutated"],
            }
    return {"ok": True, "index": None, "findings": []}
