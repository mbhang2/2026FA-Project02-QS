"""Does the strategy's behaviour depend on what the question says it depends on?

The code check (`fidelity.code_violations`) can prove a strategy *reads*
`delta`. It cannot prove the strategy *acts* on it, or acts in the direction the
question names -- "trades against the move" and "trades with it" use the same
names. That was left to a model (`scripts/fidelity_judge.py`). This module asks
the same questions of the strategy's behaviour instead, mechanically:

* **`uses_flow`** -- run the proposal on the synthetic probe data, then again on
  the same data with every aggressor side inverted and every price untouched
  (`probe-nq-shape-flipped`, `SyntheticSpec.aggressor = "flipped"`). If the two fill logs
  are byte-identical, nothing the strategy does depends on order flow, whatever
  its code computes.
* **`trades_move`** -- on the probe run's fills, compare each entry's direction
  with the price move just before it, over several lookbacks. A reversal question
  needs entries against the move at some horizon; a continuation question needs
  them with it.

**Why this is safe to run before a trial and to repair on.** Every run is on
`synthetic-nq-shape`: seeded noise shaped like NQ's regular-hours session with no relation to NQ (the argument is
`probe.py`'s, and holds here unchanged). What reaches the agent is whether its
trades moved and which way its entries leaned -- facts about synthetic noise,
never a score. So a violation can go back for repair without the repair counting
as a trial, exactly like the code check.

**When it cannot say.** A strategy that makes no trades on the probe data, or
too few entries with a move before them, is *inconclusive*, never a violation:
three of eight historical champions never traded on `synthetic-check`. A fitted
proposal is not perturbed (its probe profile has a fit window the pair lacks).
Inconclusive is recorded, so "checked and clean" and "could not check" differ.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from pathlib import Path

import yaml

from .._roots import repo_root
from .campaign import declares_fit
from .fidelity import Constraints, Violation, read_fill_rows, round_turns
from .probe import run_on_profile

REPO_ROOT = repo_root()
BASE, FLIPPED = "probe-nq-shape", "probe-nq-shape-flipped"
LOOKBACK_MINUTES = (1, 5, 15, 30, 60)
# A direction claim is about a tendency, not every entry: 60% at the strategy's
# best-matching lookback. A rule entering at random sits near 50% at every one.
DEFAULT_MOVE_SHARE = 0.6
MIN_ENTRIES = 5


def _fills(row: dict | None) -> list:
    if not row or row.get("outcome") != "completed":
        return []
    src = row.get("fill_log_source")
    if not src:
        return []
    f = Path(src)
    f = f if f.is_absolute() else REPO_ROOT / f
    return read_fill_rows(f) if f.is_file() else []


def _probe_prices(profile: str = BASE) -> dict[int, tuple[list[int], list[float]]]:
    """The probe profile's tick path per session -- regenerated, not read back:
    the dataset is seeded, so this is the exact series the probe run saw."""
    from ..data.synthetic import SyntheticDataset, SyntheticSpec
    from ..types import Split

    cfg = yaml.safe_load((REPO_ROOT / "config" / "run.yaml").read_text())
    prof = cfg["profiles"][profile]
    spec = dict(cfg["datasets"][prof["dataset"]])
    spec.pop("kind")
    ds = SyntheticDataset(SyntheticSpec(**spec, seed=prof["seed"]))
    out: dict[int, tuple[list[int], list[float]]] = defaultdict(lambda: ([], []))
    for t in ds.ticks(Split(prof["split"]), warmup=prof.get("warmup_sessions", 0)):
        out[t.session][0].append(t.ts)
        out[t.session][1].append(t.price)
    return dict(out)


def move_shares(fills: list, prices: dict) -> dict[int, tuple[int, int]]:
    """Per lookback in minutes: (entries against the prior move, entries with it).

    The move is the price at entry minus the price `L` minutes earlier in the
    same session, both as of the last tick at or before. Entries with no prior
    price that far back, or a flat move, count for neither side.
    """
    turns, _ = round_turns(fills)
    out = {}
    for lb in LOOKBACK_MINUTES:
        against = with_ = 0
        for t in turns:
            ts_list, px = prices.get(t.entry_session, ([], []))
            i = bisect.bisect_right(ts_list, t.entry_ts) - 1
            j = bisect.bisect_right(ts_list, t.entry_ts - lb * 60 * 10**9) - 1
            if i < 0 or j < 0 or j == i:
                continue
            move = px[i] - px[j]
            if move == 0:
                continue
            if (move > 0) == (t.quantity > 0):
                with_ += 1
            else:
                against += 1
        out[lb] = (against, with_)
    return out


def check(source: str, cons: Constraints, *, ledger: Path,
          strategy_path: Path | None = None) -> tuple[list[Violation], dict]:
    """The probe constraints' violations, and a report of what was checked."""
    items = [c for c in cons.items if c.check in ("uses_flow", "trades_move")]
    if not items:
        return [], {}
    if declares_fit(source):
        return [], {"ran": False, "reason": "fitted proposals are not perturbed"}
    base = run_on_profile(source, BASE, ledger=ledger, strategy_path=strategy_path)
    fills = _fills(base)
    report: dict = {"ran": True, "base_trades": len(fills) // 2}
    if not fills:
        report["inconclusive"] = "no trades on the synthetic probe data"
        return [], report
    out: list[Violation] = []
    for c in items:
        if c.check == "uses_flow":
            flipped = run_on_profile(source, FLIPPED, ledger=ledger,
                                     strategy_path=strategy_path)
            if not flipped or flipped.get("outcome") != "completed":
                report[c.id] = "inconclusive: the flipped run did not complete"
                continue
            same = flipped.get("fill_log_digest") == base.get("fill_log_digest")
            report[c.id] = "identical trades" if same else "trades changed"
            if same:
                out.append(Violation(c.id, c.check, c.says,
                    "your trades are identical when every aggressor side is "
                    "inverted and every price is unchanged, so nothing your "
                    "strategy does depends on order flow"))
        else:
            want = c.params["direction"]
            need = float(c.params.get("share", DEFAULT_MOVE_SHARE))
            shares = move_shares(fills, _probe_prices())
            counted = {lb: a + w for lb, (a, w) in shares.items()}
            if max(counted.values()) < MIN_ENTRIES:
                report[c.id] = "inconclusive: too few entries after a move"
                continue
            frac = {lb: ((a if want == "against" else w) / (a + w))
                    for lb, (a, w) in shares.items() if a + w >= MIN_ENTRIES}
            best = max(frac.values())
            report[c.id] = {f"{want} {lb}m": round(v, 2) for lb, v in frac.items()}
            if best < need:
                lean = ", ".join(f"{lb} min {v:.0%}" for lb, v in frac.items())
                out.append(Violation(c.id, c.check, c.says,
                    f"at no lookback did {need:.0%} of your entries go {want} the "
                    f"prior price move ({want} share by lookback: {lean})"))
    return out, report
