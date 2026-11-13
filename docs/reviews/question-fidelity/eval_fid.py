"""Run fidelity checks over every historical trial. args: repo archive"""
import json, sys
from pathlib import Path
repo, arch = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo))
from src.core.loop import fidelity as F

out = {}
for cdir in sorted((arch / "runs" / "campaigns").iterdir()):
    cj = cdir / "campaign.json"
    if not cj.exists() or not (cdir / "ledger.jsonl").exists(): continue
    rec = json.loads(cj.read_text()); rec = rec.get("record", rec)
    hp = rec.get("hypothesis_path")
    if not hp: continue
    hyp = repo / hp
    side = F.sidecar_for(hyp)
    if not side.exists(): continue
    cons = F.load(side, hyp.read_text())
    rows = [json.loads(l) for l in (cdir / "ledger.jsonl").read_text().splitlines() if l.strip()]
    trials = []
    for i, r in enumerate(rows):
        src = r.get("strategy_source")
        code = []
        if src:
            f = arch / src
            if not f.exists(): f = cdir / "sources" / Path(src).name
            if f.exists():
                code = [v.as_dict() for v in F.code_violations(f.read_text(), cons)]
        beh = F.check_trial(r, cons, root=arch)
        sc = r.get("score") or {}
        p = sc.get("primary") if not sc.get("rejected") else None
        has_fills = bool(r.get("fill_log_source")) and (arch / r["fill_log_source"]).exists()
        trials.append({"i": i, "score": p, "code": [v["id"] for v in code],
                       "beh": [v["id"] for v in beh], "beh_detail": [v["detail"] for v in beh],
                       "fills": has_fills, "outcome": r.get("outcome")})
    scored = [t for t in trials if t["score"] is not None]
    champ = max(scored, key=lambda t: t["score"]) if scored else None
    on_q = [t for t in scored if not t["code"] and not t["beh"]]
    out[cdir.name] = {"hyp": side.name, "n": len(trials), "scored": len(scored),
        "off": sum(1 for t in scored if t["code"] or t["beh"]),
        "no_fills": sum(1 for t in scored if not t["fills"]),
        "champ": champ, "best_on_q": max(on_q, key=lambda t: t["score"]) if on_q else None}
json.dump(out, open(sys.argv[3], "w"), indent=1, default=str)
for k, v in out.items():
    c = v["champ"] or {}
    print(f"{k:28s} scored {v['scored']:3d} off {v['off']:3d} nofills {v['no_fills']:3d} | champ #{c.get('i')} {c.get('score') if c.get('score') is None else round(c['score'],3)} code={c.get('code')} beh={c.get('beh')} {'; '.join(c.get('beh_detail') or [])[:150]}")
