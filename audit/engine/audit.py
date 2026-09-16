"""Independent audit: reproduce, leak/PIT, freeze, then PSR/DSR.

Must-haves only. The LLM critic reads this packet; it does not invent gates.
A high PSR never overrides a leak.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from audit.engine import data, leaks, stats
from audit.engine.backtest import load_strategy, run_experiment
from audit.engine.protocol import load_experiment, load_json
from audit.engine.store import ACTION_FROM_RECOMMENDATION, decide_action

IMPLAUSIBLE_ABS_SHARPE = 2.0

DEMO_CASES = {
    "kill": {
        "ticket": "research/examples/buy-hold-sanity",
        "expect": "FAIL",
        "why": "Look-ahead cheat posts a huge Sharpe; leak/PIT must FAIL.",
    },
    "pause": {
        "ticket": "research/examples/tsmom-pause",
        "expect": "PASS",
        "why": "Legal time-series momentum on synthetic bars. Auditor PASS is not PROMOTE.",
    },
}


def _metrics_close(a: dict[str, Any], b: dict[str, Any], *, tol: float = 1e-9) -> list[str]:
    problems = []
    keys = set(a) | set(b)
    for key in sorted(keys):
        av, bv = a.get(key), b.get(key)
        if isinstance(av, float) or isinstance(bv, float):
            if av is None or bv is None or not math.isclose(float(av), float(bv), rel_tol=0, abs_tol=tol):
                problems.append(f"metric {key} mismatch: {av} vs {bv}")
        elif av != bv:
            problems.append(f"metric {key} mismatch: {av} vs {bv}")
    return problems


def parse_protocol_md(text: str) -> dict[str, Any]:
    """Pull freeze fields out of PROTOCOL.md. Missing fields stay None."""
    listed = re.findall(r"experiments/([A-Za-z0-9._-]+\.json)", text)
    delay = None
    m = re.search(r"delay_bars:\s*(\d+)", text)
    if m:
        delay = int(m.group(1))
    comm = None
    m = re.search(r"commission_bps:\s*([0-9]+(?:\.[0-9]+)?)", text)
    if m:
        comm = float(m.group(1))
    start = None
    m = re.search(r"^-\s*start:\s*(\d{4}-\d{2}-\d{2})", text, re.M)
    if m:
        start = m.group(1)
    end = None
    m = re.search(r"^-\s*end:\s*(\d{4}-\d{2}-\d{2})", text, re.M)
    if m:
        end = m.group(1)
    budget = None
    m = re.search(r"max experiments(?: this ticket)?:\s*(\d+)", text, re.I)
    if m:
        budget = int(m.group(1))
    return {
        "listed": listed,
        "delay_bars": delay,
        "commission_bps": comm,
        "start": start,
        "end": end,
        "max_experiments": budget,
    }


def _pit_all_symbols(strategy, exp: dict[str, Any], *, seed: int) -> dict[str, Any]:
    findings: list[str] = []
    ok = True
    last_index = None
    for symbol in exp["universe"]:
        bars = data.bars_for(exp, symbol, seed=seed)
        closes = data.closes(bars)
        pit = leaks.point_in_time_ok(strategy.signal, closes)
        if not pit["ok"]:
            ok = False
            last_index = pit.get("index")
            tagged = [f"{symbol}: {f}" for f in (pit.get("findings") or [])]
            findings.extend(tagged or [f"{symbol}: PIT fail"])
    return {"ok": ok, "index": last_index, "findings": findings}


def _execution_findings(exp: dict[str, Any], protocol: dict[str, Any] | None) -> list[str]:
    findings: list[str] = []
    kind = exp.get("kind")
    costs = exp["costs"]
    delay = int(costs["delay_bars"])
    commission = float(costs["commission_bps"])
    if kind == "realistic" and delay < 1:
        findings.append("realistic experiment has delay_bars < 1")
    if kind == "realistic" and commission <= 0:
        findings.append("realistic experiment has commission_bps <= 0")
    if kind == "realistic" and protocol:
        proto_delay = protocol.get("delay_bars")
        if proto_delay is not None and delay < int(proto_delay):
            findings.append(
                f"realistic delay_bars={delay} is below PROTOCOL delay_bars={proto_delay}"
            )
        proto_comm = protocol.get("commission_bps")
        if proto_comm is not None and commission + 1e-12 < float(proto_comm):
            findings.append(
                f"realistic commission_bps={commission} is below PROTOCOL commission_bps={proto_comm}"
            )
        proto_start = protocol.get("start")
        proto_end = protocol.get("end")
        if proto_start and exp["start"] != proto_start:
            findings.append(f"start {exp['start']} disagrees with PROTOCOL {proto_start}")
        if proto_end and exp["end"] != proto_end:
            findings.append(f"end {exp['end']} disagrees with PROTOCOL {proto_end}")
    return findings


def _stats_packet(
    run: dict[str, Any],
    *,
    kind: str | None,
    n_trials: int,
    exp: dict[str, Any],
) -> dict[str, Any]:
    pnl = run.get("pnl") or []
    spec = data.experiment_data_spec(exp)
    ppy = 252
    if spec["frequency"] != "1d" and pnl:
        span_days = max((data.parse_iso(exp["end"]) - data.parse_iso(exp["start"])).days, 1)
        ppy = max(1, int(round(len(pnl) / (span_days / 365.25))))
    packet = stats.track_record(pnl, n_trials=n_trials, periods_per_year=ppy)
    if kind == "sanity":
        packet["stats_gate"] = "SKIP"
        return packet
    sharpe = float(run["metrics"].get("sharpe") or 0.0)
    if kind == "realistic" and data.is_synthetic(exp) and abs(sharpe) > IMPLAUSIBLE_ABS_SHARPE:
        packet["stats_gate"] = "FAIL"
    return packet


def audit_experiment(
    path: str | Path,
    *,
    seed: int = 7,
    previous: dict[str, Any] | None = None,
    n_trials: int = 1,
    protocol: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = Path(path)
    exp = load_experiment(path)
    run = run_experiment(exp, seed=seed, include_series=True)
    findings: list[str] = []
    kind = exp.get("kind")
    leak = run["leak"]
    gates = {
        "reproduce": "PASS",
        "leak": "PASS",
        "pit": "PASS",
        "execution": "PASS",
        "freeze": "PASS",
        "stats": "SKIP",
        "overall": "PASS",
    }

    negative_control = kind == "sanity" and leak["verdict"] == "FAIL"
    if leak["verdict"] == "FAIL" and not negative_control:
        gates["leak"] = "FAIL"
        findings.extend(leak.get("findings") or ["leak FAIL"])

    if previous:
        mismatches = _metrics_close(previous.get("metrics") or {}, run["metrics"])
        if mismatches:
            gates["reproduce"] = "FAIL"
            findings.extend(mismatches)

    strategy = load_strategy(exp["strategy"])
    pit = _pit_all_symbols(strategy, exp, seed=seed)
    if not pit["ok"] and kind != "sanity":
        gates["pit"] = "FAIL"
        findings.extend(pit["findings"])

    exec_findings = _execution_findings(exp, protocol)
    if exec_findings:
        if any("PROTOCOL" in f or "disagrees" in f for f in exec_findings):
            gates["freeze"] = "FAIL"
        if any("delay_bars" in f or "commission_bps" in f for f in exec_findings):
            gates["execution"] = "FAIL"
        findings.extend(exec_findings)

    packet = _stats_packet(run, kind=kind, n_trials=n_trials, exp=exp)
    gates["stats"] = packet["stats_gate"]
    if packet["stats_gate"] == "FAIL":
        sharpe = float(run["metrics"].get("sharpe") or 0.0)
        findings.append(
            f"implausible |sharpe|={sharpe:.3f} > {IMPLAUSIBLE_ABS_SHARPE} on synthetic bars"
        )

    hard = {k: v for k, v in gates.items() if k not in ("stats", "overall")}
    overall = "FAIL" if any(v == "FAIL" for v in hard.values()) else "PASS"
    if gates["stats"] == "FAIL":
        overall = "FAIL"
    gates["overall"] = overall
    row = {
        "experiment": str(path),
        "experiment_id": exp["id"],
        "kind": kind,
        "data_source": data.experiment_data_spec(exp)["source"],
        "verdict": overall,
        "findings": findings,
        "leak": leak,
        "metrics": run["metrics"],
        "pit_ok": pit["ok"],
        "gates": gates,
        "stats": packet,
        "critic_allowed": overall == "PASS",
        "run": {k: v for k, v in run.items() if k != "pnl"},
    }
    action, next_step = decide_action(row)
    row["action"] = action
    row["next_step"] = next_step
    return row


def _ticket_freeze(rows: list[dict[str, Any]], protocol: dict[str, Any] | None, paths: list[Path]) -> list[str]:
    findings: list[str] = []
    kinds = [r.get("kind") for r in rows]
    if "realistic" in kinds and "replicate" not in kinds and "sanity" not in kinds:
        findings.append("realistic run without a replicate experiment")

    fingerprints = []
    for path in paths:
        exp = load_json(path)
        fingerprints.append(
            (
                tuple(exp.get("universe") or []),
                exp.get("start"),
                exp.get("end"),
                tuple(exp.get("metrics") or []),
            )
        )
    if fingerprints and any(fp != fingerprints[0] for fp in fingerprints[1:]):
        findings.append("experiments disagree on universe/start/end/metrics")

    if not protocol:
        return findings
    listed = protocol.get("listed") or []
    present = {p.name for p in paths}
    if listed:
        missing = [name for name in listed if name not in present]
        extra = sorted(present - set(listed))
        if missing:
            findings.append(f"PROTOCOL lists missing experiments: {missing}")
        if extra:
            findings.append(f"unlisted experiment files (possible undeclared trials): {extra}")
    budget = protocol.get("max_experiments")
    if budget is not None and len(paths) > int(budget):
        findings.append(f"trial budget exceeded: {len(paths)} experiments > max {budget}")
    return findings


def _recommendation(*, overall: str, rows: list[dict[str, Any]]) -> str:
    if overall != "FAIL":
        return "PAUSE"
    leak_or_pit = any(
        (row.get("gates") or {}).get("leak") == "FAIL" or (row.get("gates") or {}).get("pit") == "FAIL"
        for row in rows
        if row.get("experiment_id") != "protocol"
    )
    implausible = any((row.get("gates") or {}).get("stats") == "FAIL" for row in rows)
    if leak_or_pit or implausible:
        return "DISCARD"
    return "ITERATE"


def _write_results_tsv(ticket_dir: Path, packet: dict[str, Any]) -> None:
    reports = ticket_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    dest = reports / "results.tsv"
    header = (
        "ticket\texperiment_id\tkind\tn_trials\tsharpe\tpsr\tdsr\tleak\tpit\t"
        "gate\tstatus\tfindings\n"
    )
    lines = [header]
    ticket = ticket_dir.name
    n_trials = packet.get("n_trials")
    for row in packet.get("experiments") or []:
        st = row.get("stats") or {}
        leak = (row.get("leak") or {}).get("verdict") or ""
        findings = "; ".join(row.get("findings") or [])
        lines.append(
            "\t".join(
                [
                    ticket,
                    str(row.get("experiment_id") or ""),
                    str(row.get("kind") or ""),
                    str(n_trials or ""),
                    _fmt(st.get("sharpe_annual", (row.get("metrics") or {}).get("sharpe"))),
                    _fmt(st.get("psr")),
                    _fmt(st.get("dsr")),
                    str(leak),
                    "PASS" if row.get("pit_ok") else "FAIL",
                    str((row.get("gates") or {}).get("overall") or row.get("verdict") or ""),
                    str(row.get("verdict") or ""),
                    findings.replace("\t", " ").replace("\n", " "),
                ]
            )
            + "\n"
        )
    dest.write_text("".join(lines), encoding="utf-8")


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def slim_audit(packet: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe packet without per-bar series."""
    out = {k: v for k, v in packet.items() if k != "experiments"}
    slim_rows = []
    for row in packet.get("experiments") or []:
        slim_rows.append({kk: vv for kk, vv in row.items() if kk != "run"})
    out["experiments"] = slim_rows
    return out


# What this module's `n_trials` actually counts, recorded in every packet and
# every ledger row it produces (Task 42).
#
# It is `len(glob("experiments/*.json"))` -- the experiment files somebody left
# in a ticket directory. That is a reasonable thing for an auditor to deflate by:
# it is the trials the auditor can see. It is **not** this project's N, and the
# difference is worth stating wherever the number appears, because a deflated
# Sharpe quoted against the wrong N is a number that looks corrected and is not.
#
# Three differences, none of them a defect in this code:
#
#   * it is reconstructed from the filesystem after the fact, where
#     `runs/ledger.jsonl` is written as the trials happen -- CLAUDE.md's phrase
#     is "logged rather than estimated after the fact";
#   * it shrinks when a file is deleted, so removing a losing experiment lowers
#     the deflation bar and improves whatever survives;
#   * it is per ticket, where this project's N is global across every dataset,
#     because otherwise adding a dataset silently buys a fresh holdout.
N_TRIALS_PROVENANCE = (
    "count of experiments/*.json in the ticket directory at audit time; "
    "reconstructed from the filesystem, per-ticket, and NOT the project's N "
    "(runs/ledger.jsonl, global, written as trials happen)"
)


def audit_ticket(
    experiment_paths: list[Path],
    *,
    seed: int = 7,
    previous_by_id: dict[str, Any] | None = None,
    ticket_dir: Path | None = None,
) -> dict[str, Any]:
    previous_by_id = previous_by_id or {}
    paths = [Path(p) for p in experiment_paths]
    protocol = None
    if ticket_dir is not None:
        proto_path = Path(ticket_dir) / "PROTOCOL.md"
        if proto_path.is_file():
            protocol = parse_protocol_md(proto_path.read_text(encoding="utf-8"))

    n_trials = max(len(paths), 1)
    rows = []
    overall = "PASS"
    for path in paths:
        prev = previous_by_id.get(path.stem) or previous_by_id.get(path.name)
        if prev is None and previous_by_id:
            # reports often key by experiment id, not filename
            loaded = load_json(path) if path.is_file() else {}
            prev = previous_by_id.get(loaded.get("id") or "")
        row = audit_experiment(
            path, seed=seed, previous=prev, n_trials=n_trials, protocol=protocol
        )
        rows.append(row)
        if row["verdict"] == "FAIL":
            overall = "FAIL"

    freeze_findings = _ticket_freeze(rows, protocol, paths)
    if freeze_findings:
        overall = "FAIL"
        rows.append(
            {
                "experiment": "",
                "experiment_id": "protocol",
                "kind": None,
                "verdict": "FAIL",
                "findings": freeze_findings,
                "leak": None,
                "metrics": {},
                "pit_ok": True,
                "gates": {
                    "reproduce": "PASS",
                    "leak": "PASS",
                    "pit": "PASS",
                    "execution": "PASS",
                    "freeze": "FAIL",
                    "stats": "SKIP",
                    "overall": "FAIL",
                },
                "stats": {},
                "critic_allowed": False,
                "action": "modify",
                "next_step": "Continue only after modifications: " + "; ".join(freeze_findings),
                "run": {},
            }
        )

    recommendation = _recommendation(overall=overall, rows=rows)
    packet = {
        "verdict": overall,
        "n_trials": n_trials,
        "n_trials_provenance": N_TRIALS_PROVENANCE,
        "gates": {
            "overall": overall,
            "critic_allowed": overall == "PASS",
        },
        "recommendation": recommendation,
        "action": ACTION_FROM_RECOMMENDATION.get(recommendation, "pause"),
        "experiments": rows,
    }
    if ticket_dir is not None:
        _write_results_tsv(Path(ticket_dir), packet)
    return packet


def _load_previous_reports(ticket_dir: Path) -> dict[str, Any]:
    previous: dict[str, Any] = {}
    reports = ticket_dir / "reports"
    if not reports.is_dir():
        return previous
    skip = {"audit.json"}
    for path in reports.glob("*.json"):
        if path.name in skip:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "metrics" not in payload:
            continue
        exp_id = payload.get("experiment_id") or path.stem
        previous[str(exp_id)] = payload
        previous[path.stem] = payload
        previous[path.name] = payload
    return previous


def audit_ticket_dir(ticket_dir: str | Path, *, seed: int = 7) -> dict[str, Any]:
    ticket_dir = Path(ticket_dir)
    exp_dir = ticket_dir / "experiments"
    if not exp_dir.is_dir():
        raise FileNotFoundError(f"no experiments/ under {ticket_dir}")
    paths = sorted(p for p in exp_dir.glob("*.json") if p.is_file())
    if not paths:
        raise FileNotFoundError(f"no experiment JSON in {exp_dir}")
    previous = _load_previous_reports(ticket_dir)
    return audit_ticket(paths, seed=seed, previous_by_id=previous, ticket_dir=ticket_dir)


def list_experiments(root: Path | None = None) -> list[dict[str, str]]:
    """Map experiment_id → JSON path. Demo tickets (research/examples) first."""
    root = root or Path.cwd()
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    search_roots = [root / "research" / "examples", root / "research"]
    for base in search_roots:
        if not base.is_dir():
            continue
        for path in sorted(base.glob("**/experiments/*.json")):
            if "_template" in path.parts:
                continue
            try:
                exp = load_json(path)
            except Exception:
                continue
            exp_id = str(exp.get("id") or path.stem)
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            ticket = path.parent.parent
            rows.append(
                {
                    "experiment_id": exp_id,
                    "path": str(path),
                    "ticket": str(ticket),
                    "kind": str(exp.get("kind") or ""),
                }
            )
    return rows


def find_experiment(experiment_id: str, *, root: Path | None = None) -> dict[str, str]:
    """Resolve an experiment_id. Prefers research/examples for the demo."""
    matches = [r for r in list_experiments(root) if r["experiment_id"] == experiment_id]
    if not matches:
        known = sorted({r["experiment_id"] for r in list_experiments(root)})
        raise FileNotFoundError(
            f"no experiment_id {experiment_id!r}; known: {known}"
        )
    examples = [m for m in matches if "research/examples" in m["path"].replace("\\", "/")]
    chosen = (examples or matches)[0]
    return chosen


def audit_experiment_id(
    experiment_id: str,
    *,
    seed: int = 7,
    root: Path | None = None,
) -> dict[str, Any]:
    found = find_experiment(experiment_id, root=root)
    ticket_dir = Path(found["ticket"])
    previous = _load_previous_reports(ticket_dir) if (ticket_dir / "reports").is_dir() else {}
    protocol = None
    proto_path = ticket_dir / "PROTOCOL.md"
    if proto_path.is_file():
        protocol = parse_protocol_md(proto_path.read_text(encoding="utf-8"))
    n_trials = len(list((ticket_dir / "experiments").glob("*.json"))) or 1
    prev = previous.get(experiment_id) or previous.get(Path(found["path"]).stem)
    row = audit_experiment(
        found["path"],
        seed=seed,
        previous=prev,
        n_trials=n_trials,
        protocol=protocol,
    )
    return {
        "verdict": row["verdict"],
        "n_trials": n_trials,
        "n_trials_provenance": N_TRIALS_PROVENANCE,
        "recommendation": None,
        "action": row["action"],
        "experiments": [row],
        "gates": {"overall": row["verdict"], "critic_allowed": row["verdict"] == "PASS"},
        "lookup": found,
    }


def list_demos() -> dict[str, Any]:
    return {"cases": DEMO_CASES}


def run_demo(case: str, *, seed: int = 7, root: Path | None = None) -> dict[str, Any]:
    if case not in DEMO_CASES:
        raise ValueError(f"unknown demo case {case!r}; choose {sorted(DEMO_CASES)}")
    info = DEMO_CASES[case]
    root = root or Path.cwd()
    ticket = root / info["ticket"]
    packet = audit_ticket_dir(ticket, seed=seed)
    packet["demo"] = {"case": case, **info, "ticket": str(ticket)}
    return packet
