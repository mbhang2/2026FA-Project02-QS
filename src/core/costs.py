"""Fees and slippage per instrument, from `config/costs.yaml`.

A profile with `costs: <instrument>` takes its rates from that file. The rates land in
the same profile keys the engine and scorers already read, so there is one path from a
configured rate to a charged one. A profile that names an instrument and also states a
rate is refused: two sources for one number is how a run gets priced differently from
what its record says.

**Slippage is charged as a cost and never moved into the fill price.** The sign-flip null
flips a round turn's gross P&L and charges its cost again. Slippage inside the price would
be flipped into a gain on half the draws, which would make the null too generous.
"""

from __future__ import annotations

import math
from pathlib import Path

import yaml

from ._roots import repo_root

COSTS_PATH = repo_root() / "config" / "costs.yaml"
FEE_KEYS = {"per_contract": "commission_per_contract", "bps_notional": "commission_bps"}
SLIPPAGE_KEYS = {"per_contract": "slippage_per_contract", "bps_notional": "slippage_bps"}
MAKER_KEYS = {"per_contract": "maker_commission_per_contract",
              "bps_notional": "maker_commission_bps"}
COST_KEYS = (*FEE_KEYS.values(), *MAKER_KEYS.values(), *SLIPPAGE_KEYS.values())
ROW_COST_KEYS = (*MAKER_KEYS.values(), *SLIPPAGE_KEYS.values())


class CostConfigError(ValueError):
    """The cost file or a profile's use of it is not valid."""


def _rate(name: str, kind: str, spec, keys: dict[str, str]) -> tuple[str, float]:
    if not isinstance(spec, dict) or set(spec) != {"model", "rate"}:
        raise CostConfigError(f"{name}: {kind} must have exactly `model` and `rate`")
    if spec["model"] not in keys:
        raise CostConfigError(f"{name}: unknown {kind} model {spec['model']!r}; "
                              f"have {sorted(keys)}")
    rate = spec["rate"]
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) \
            or not math.isfinite(rate) or rate < 0:
        raise CostConfigError(f"{name}: {kind} rate must be finite and >= 0, got {rate!r}")
    return keys[spec["model"]], float(rate)


def _fee(name: str, spec) -> dict[str, float]:
    """A fee's taker and maker rates. A negative maker rate is a rebate."""
    if not isinstance(spec, dict) or set(spec) != {"model", "maker", "taker"}:
        raise CostConfigError(f"{name}: fee must have exactly `model`, `maker` and `taker`")
    if spec["model"] not in FEE_KEYS:
        raise CostConfigError(f"{name}: unknown fee model {spec['model']!r}; "
                              f"have {sorted(FEE_KEYS)}")
    for side in ("maker", "taker"):
        value = spec[side]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            raise CostConfigError(f"{name}: fee {side} must be a finite number, got {value!r}")
    maker, taker = float(spec["maker"]), float(spec["taker"])
    if taker < 0:
        raise CostConfigError(f"{name}: the taker fee must be >= 0, got {taker}")
    if maker + taker < 0:
        raise CostConfigError(f"{name}: a maker rebate of {maker} exceeds the {taker} taker "
                              "fee, so fees alone would pay a round trip")
    return {FEE_KEYS[spec["model"]]: taker, MAKER_KEYS[spec["model"]]: maker}


def terms_for(name: str, entry: dict) -> dict[str, float]:
    """The rates one registry entry gives."""
    unknown = set(entry) - {"asset_class", "fee", "slippage"}
    if unknown or "fee" not in entry or "slippage" not in entry:
        raise CostConfigError(f"{name}: needs `fee` and `slippage`; unknown keys {sorted(unknown)}")
    out = dict.fromkeys(COST_KEYS, 0.0)
    out.update(_fee(name, entry["fee"]))
    key, rate = _rate(name, "slippage", entry["slippage"], SLIPPAGE_KEYS)
    out[key] = rate
    return out


def load_registry(path: Path | None = None) -> dict[str, dict[str, float]]:
    data = yaml.safe_load((path or COSTS_PATH).read_text()) or {}
    instruments = data.get("instruments")
    if not isinstance(instruments, dict) or not instruments:
        raise CostConfigError("the cost file has no `instruments`")
    return {name: terms_for(name, entry) for name, entry in instruments.items()}


def apply_costs(cfg: dict, path: Path | None = None) -> dict:
    """Fill each `costs:` profile's rates from the registry, in place. Returns `cfg`."""
    named = {n: p for n, p in (cfg.get("profiles") or {}).items() if "costs" in p}
    if not named:
        return cfg
    registry = load_registry(path)
    for name, profile in named.items():
        instrument = profile["costs"]
        if instrument not in registry:
            raise CostConfigError(f"profile {name!r}: no instrument {instrument!r} in the cost "
                                  f"file; have {sorted(registry)}")
        inline = [k for k in COST_KEYS if k in profile]
        if inline:
            raise CostConfigError(f"profile {name!r} names costs {instrument!r} and also "
                                  f"states {inline}. Keep the rate in one place.")
        symbol = (cfg.get("datasets") or {}).get(profile.get("dataset"), {}).get("symbol")
        if symbol is not None and symbol != instrument:
            raise CostConfigError(f"profile {name!r} trades {symbol} and is priced as "
                                  f"{instrument}")
        profile.update(registry[instrument])
    return cfg


def row_cost_terms(row: dict) -> dict:
    """The maker and slippage rates a recorded run actually paid, to rebuild its blotter.

    `None` for a maker rate means the run charged makers the taker rate."""
    execution = row.get("execution") or {}
    return {"maker_commission_per_contract": execution.get("maker_commission_per_contract"),
            "maker_commission_bps": execution.get("maker_commission_bps"),
            "slippage_per_contract": float(execution.get("slippage_per_contract", 0.0)),
            "slippage_bps": float(execution.get("slippage_bps", 0.0))}
