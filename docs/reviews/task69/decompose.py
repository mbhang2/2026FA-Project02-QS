"""Task 69 — decompose Sortino's denominator by idle vs active sessions.

Pre-registration: docs/reviews/task69/README.md (6e5c3c5). Reads nq-ml-02's fill
logs and the scored-window calendar; touches nothing the campaign imports.
"""
from __future__ import annotations
import json, math, pathlib, sys
REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "scripts"))
import yaml
from replay import load_fill_log, rebuild_dataset           # noqa: E402
from src.core.engine.reconstruct import reconstruct         # noqa: E402
from src.core.scoring import metrics as M                   # noqa: E402
from src.core.scoring.scorer import score_returns           # noqa: E402
from src.core.types import Split                            # noqa: E402

CAMPAIGN = "nq-ml-02"
CHARGE = 0.18
MAR = M.MAR

def sortino(returns, denom_over=None):
    """Sortino as the gate computes it, with the denominator's session set
    controllable. `denom_over=None` -> all sessions (the gate). Verified against
    `score_returns` on the unmodified series before being trusted."""
    n = len(returns)
    mean = math.fsum(returns) / n
    ds = returns if denom_over is None else [returns[i] for i in denom_over]
    sq = [min(r - MAR, 0.0) ** 2 for r in ds]
    dd = math.sqrt(math.fsum(sq) / len(ds))
    return mean / dd if dd > 0 else float("nan")

def main():
    cfg = yaml.safe_load((REPO / "config" / "run.yaml").read_text())
    rows = [json.loads(l) for l in (REPO / "runs" / "campaigns" / CAMPAIGN / "ledger.jsonl").read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r.get("outcome") == "completed" and r.get("fill_log_source")]
    prof = cfg["profiles"][rows[0]["profile"]] if rows and "profile" in rows[0] else cfg["profiles"]["nq-train-fitted"]
    comm = prof["commission_per_contract"]; nb = prof.get("notional_base", 1.0)
    out = []
    print(f"{'trial':>5} {'active':>6} {'ledger':>8} {'replay':>8} {'match':>5} {'uncharged':>9} {'active-dd':>9} {'idle dd%':>8}")
    for i, row in enumerate(rows):
        fills = load_fill_log(row)
        blotter = reconstruct(fills, commission_per_contract=comm)
        ds = rebuild_dataset(row, cfg)
        # Exactly Engine._split_sessions(): the dataset's own scored_sessions()
        # first (NQ returns native ordinal ids), sessions_for() only as the
        # fallback (synthetic, index ids). Calling the fallback on NQ yields
        # indices 1..384, and session_bounds(285) silently computes a span in
        # year 1 -- which is how the first version of this script matched 0/10.
        warm = int(row.get("warmup_sessions") or 0)
        own = getattr(ds, "scored_sessions", None)
        if own is not None:
            live = list(own(Split(row["split"]), warm))
        else:
            from src.core.data.dataset import sessions_for
            live = list(sessions_for(ds.calendar(), Split(row["split"]), warm)[1])
        scored = live[int(row.get("fit_sessions") or 0):]
        cal = M.session_calendar(ds, scored)
        r_ch = M.session_returns(blotter, cal, notional_base=nb, idle_charge=CHARGE)
        r_un = M.session_returns(blotter, cal, notional_base=nb, idle_charge=0.0)
        # 1. does the replay reproduce the run?
        led = row["score"]["primary"]
        gate = score_returns(r_ch).sortino
        match = abs(gate - led) < 1e-9
        # 2. does the hand formula reproduce the gate? (else the decomposition is meaningless)
        hand = sortino(r_ch)
        assert abs(hand - gate) < 1e-9, f"trial {i}: hand Sortino {hand} != gate {gate}"
        # 3. the decomposition
        active_sessions = {f.session for f in fills}
        active_idx = [k for k, s in enumerate(scored) if s in active_sessions]
        idle_idx = [k for k, s in enumerate(scored) if s not in active_sessions]
        s_act = sortino(r_ch, denom_over=active_idx) if active_idx else float("nan")
        sq_all = math.fsum(min(r - MAR, 0.0) ** 2 for r in r_ch)
        sq_idle = math.fsum(min(r_ch[k] - MAR, 0.0) ** 2 for k in idle_idx)
        share = sq_idle / sq_all if sq_all > 0 else float("nan")
        s_un = sortino(r_un)
        out.append({"trial": i, "active_sessions": len(active_idx), "n_sessions": len(scored),
                    "sortino_ledger": led, "sortino_charged_replay": gate, "reproduces": match,
                    "sortino_uncharged": s_un, "sortino_active_only_dd": s_act, "dd_share_idle": share})
        print(f"{i:>5} {len(active_idx):>6} {led:>+8.4f} {gate:>+8.4f} {'yes' if match else 'NO':>5} {s_un:>+9.4f} {s_act:>+9.4f} {100*share:>7.1f}%")
    (REPO / "docs" / "reviews" / "task69" / "decompose.json").write_text(json.dumps(out, indent=2) + "\n")
    ok = all(o["reproduces"] for o in out)
    print(f"\nreplay reproduces the ledger on {sum(o['reproduces'] for o in out)}/{len(out)} trials", "" if ok else "-- DO NOT TRUST THE REST")
    raise SystemExit(0 if ok else 1)

if __name__ == "__main__":
    main()
