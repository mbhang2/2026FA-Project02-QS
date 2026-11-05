#!/usr/bin/env python3
"""The shadow reviewer: does this candidate honour what its question fixes?

    python scripts/fidelity_judge.py --source runs/campaigns/x/champion.py \
        --hypothesis config/hypotheses/flow-continuation.md
    python scripts/fidelity_judge.py --eval cases.json --repeat 2

**What it is for.** `src/core/loop/fidelity.py` checks what a machine can --
which features the code uses, hold times, entry times, session carry. It cannot
check "not a fade" or "it must condition on the opening range", and those are
where the question's meaning lives: `nq-flowcont-01` spent its budget fading the
flow its question said continues, and only the economics seat, at the end,
noticed. Each such requirement is listed under `judged:` in the hypothesis's
constraints file, quoted verbatim, and this asks a model about exactly those.

**Shadow only.** A campaign registered with `fidelity_judge: shadow` calls this
after every trial and appends to `fidelity-judge.jsonl`. Nothing reads that file
to decide anything -- not the champion, not the gate, not the agent's history.
`scripts/review_strategy.py` measured the last reviewer of this kind at Fisher
p = 1.000 against the one outcome it could be checked on; this one is held to the
same standard before it may act. `--eval` is that measurement.

**Why it should do better than that one did.** It answers per requirement rather
than giving one overall verdict; it reads the code with comments and docstrings
removed; and a VIOLATED must quote a line that is in the code, or it is recorded
as CANNOT_TELL (`fidelity.parse_judgement`).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.loop import fidelity as F                       # noqa: E402

PROMPT = ROOT / "config" / "prompts" / "fidelity-judge.md"
# A different family from the proposer (deepseek-v4-pro), and fast enough to
# run once per trial: mimo-v2.6-pro's measured 677 s would add hours to a
# campaign. The model `review_strategy.py` chose for the same reason.
MODEL = "gemini-2.5-pro"
MAX_TOKENS = 16_384
MAX_FORMAT_RETRIES = 2
MAX_SOURCE_CHARS = 24_000


def payload(stripped: str, cons: F.Constraints) -> str:
    reqs = "\n".join(f"- id `{j.id}`: \"{j.says}\"" for j in cons.judged)
    return ("# The requirements, quoted from the research question\n" + reqs +
            "\n\n# The strategy's source, comments and docstrings removed\n"
            "```python\n" + stripped[:MAX_SOURCE_CHARS] + "\n```\n")


def make_judge(model: str = MODEL):
    """A callable `(source, cons) -> dict`, with the model preflighted once."""
    from audit.committee.llm import Client, LLMUnavailable, preflight

    served = preflight(model)
    client = Client(model=model, temperature=0.0, max_tokens=MAX_TOKENS)
    system = PROMPT.read_text()

    def judge(source: str, cons: F.Constraints) -> dict:
        if not cons.judged:
            return {"ran": False, "reason": "no judged requirements"}
        try:
            stripped = F.strip_for_review(source)
        except SyntaxError:
            return {"ran": False, "reason": "does not parse"}
        base = payload(stripped, cons)
        reply = last = None
        attempts = 0
        for i in range(MAX_FORMAT_RETRIES + 1):
            attempts += 1
            try:
                reply = client.ask(system, base + ("" if i == 0 else
                    "\n\nYour last reply was not usable JSON. Return ONLY the "
                    "JSON object; write line breaks inside strings as \\n.\n"))
                break
            except LLMUnavailable as exc:
                last = exc
        if reply is None:
            # The reviewer never ran -- not a CANNOT_TELL about the code.
            return {"ran": False, "reason": f"{type(last).__name__}: {last}",
                    "model": model, "model_served": served, "attempts": attempts}
        verdicts = F.parse_judgement(reply, cons, stripped)
        return {"ran": True, "model": model, "model_served": served,
                "attempts": attempts, "verdicts": verdicts,
                "violated": [v["id"] for v in verdicts if v["verdict"] == "VIOLATED"]}

    judge.model = model                       # type: ignore[attr-defined]
    return judge


def _cons_for(hypothesis: pathlib.Path) -> F.Constraints:
    side = F.sidecar_for(hypothesis)
    return F.load(side, hypothesis.read_text())


def evaluate(cases: list[dict], repeat: int, model: str) -> dict:
    """Each case: {"name", "source", "hypothesis", "expect": {id: bool violated}}.

    Reports, per requirement verdict, how often a planted or known violation was
    called VIOLATED (catch), how often a faithful file was (false alarm), and
    how often two runs on the same file agreed.
    """
    judge = make_judge(model)
    rows = []
    for c in cases:
        cons = _cons_for(ROOT / c["hypothesis"])
        src = (ROOT / c["source"]).read_text() if "source" in c else c["text"]
        runs = [judge(src, cons) for _ in range(repeat)]
        for jid, want in c["expect"].items():
            got = [next((v["verdict"] for v in r.get("verdicts", []) if v["id"] == jid),
                        "DID_NOT_RUN") for r in runs]
            rows.append({"case": c["name"], "id": jid, "violated": want, "got": got})
        print(f"  {c['name']}: " + "; ".join(
            f"{r['id']}={'/'.join(r['got'])}" for r in rows if r["case"] == c["name"]),
            flush=True)
    pos = [r for r in rows if r["violated"]]
    neg = [r for r in rows if not r["violated"]]
    calls = lambda rs: [g for r in rs for g in r["got"]]          # noqa: E731
    agree = [len(set(r["got"])) == 1 for r in rows if len(r["got"]) > 1]
    return {
        "rows": rows,
        "catch": f"{calls(pos).count('VIOLATED')}/{len(calls(pos))}",
        "false_alarm": f"{calls(neg).count('VIOLATED')}/{len(calls(neg))}",
        "cannot_tell": f"{sum(g == 'CANNOT_TELL' for g in calls(rows))}/{len(calls(rows))}",
        "agreement": f"{sum(agree)}/{len(agree)}",
        "model": model,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=pathlib.Path)
    ap.add_argument("--hypothesis", type=pathlib.Path)
    ap.add_argument("--eval", type=pathlib.Path, help="a JSON list of cases")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--out", type=pathlib.Path, default=None)
    a = ap.parse_args()
    if a.eval:
        res = evaluate(json.loads(a.eval.read_text()), a.repeat, a.model)
    elif a.source and a.hypothesis:
        res = make_judge(a.model)(a.source.read_text(), _cons_for(a.hypothesis))
    else:
        ap.error("--source and --hypothesis, or --eval")
    text = json.dumps(res, indent=2)
    if a.out:
        a.out.write_text(text + "\n")
    print(text if not a.eval else
          {k: v for k, v in res.items() if k != "rows"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
