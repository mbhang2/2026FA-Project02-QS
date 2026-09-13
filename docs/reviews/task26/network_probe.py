#!/usr/bin/env python3
"""Can a strategy reach the network? Three probes, run through the real harness.

    python docs/reviews/task26/network_probe.py

The hole neither adversarial pass looked at. A strategy that fetches real NQ
prices for the evaluation dates from any public source, and trades on them,
produces a spectacular result with a genuine blotter, a stable dataset digest, a
clean scoring fingerprint and a replay that reproduces -- because the replay
fetches them again. It never touches the engine, the dataset or the scorer, so
every defence built in Tasks 21 through 25 is orthogonal to it.

Both passes reasoned about the object graph, because both briefs framed the
boundary as an in-process one. That is worth recording: the reachability walk is
a good tool that answers exactly one question.

Three probes, because they fail in different places:

    DNS        socket.getaddrinfo("example.com", 80)
    raw TCP    connect to a literal IP, no name resolution involved
    HTTP       urllib, the way a strategy would actually write it

A configuration that denies one and not the others is worse than none, because it
reads as protection.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
STRATEGY = REPO / "src" / "strategies" / "current.py"

PROBES = {
    "dns": "socket.getaddrinfo('example.com', 80)",
    "tcp": "socket.create_connection(('1.1.1.1', 80), timeout=5).close()",
    "http": ("urllib.request.urlopen('http://1.1.1.1/', timeout=5).read(16)"),
}

TEMPLATE = '''
import socket, urllib.request


class Strategy:
    def setup(self, ctx):
        ctx.subscribe("5m")
        {probe}

    def on_bars(self, ctx): pass
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
'''


def run(probe: str, sandbox: bool = True) -> dict:
    original = STRATEGY.read_text()
    led = pathlib.Path(tempfile.mkdtemp()) / "ledger.jsonl"
    STRATEGY.write_text(TEMPLATE.format(probe=probe))
    env = dict(os.environ)
    if not sandbox:
        env["HARNESS_NO_SANDBOX"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, "run.py", "--human", "--quiet", "--purpose", "trial",
             "--ledger", str(led)],
            capture_output=True, text=True, cwd=REPO, env=env,
        )
    finally:
        STRATEGY.write_text(original)
    rows = [json.loads(x) for x in led.read_text().splitlines() if x.strip()]
    return {"exit": proc.returncode, "row": rows[0] if rows else {}}


def main() -> int:
    # The control. A test that only shows three refusals cannot tell a working
    # sandbox from a machine with no network, and "all three failed" would read
    # as protection either way.
    print("control -- the same probes with isolation switched off:")
    for name, probe in PROBES.items():
        row = run(probe, sandbox=False)["row"]
        error = (row.get("error") or "").splitlines()
        first = error[0] if error else ""
        # A refusal looks specific: EPERM from the sandbox, or a resolver that
        # cannot see a nameserver. Anything else -- a TLS redirect, a 404, a
        # timeout -- means the packet left the machine, which is the thing being
        # measured. Classifying "any error" as blocked is how a control arm ends
        # up agreeing with whatever it was meant to check.
        denied = ("Operation not permitted" in first
                  or "nodename nor servname" in first
                  or "Temporary failure in name resolution" in first)
        got = first[:64] if denied else (
            "reached the network" + (f"  ({first[:44]})" if first else ""))
        print(f"  {name:<6} {got}")
    print()

    print(f"{'probe':<6} {'exit':>5} {'outcome':<10} {'failure_kind':<28} error")
    reached = []
    for name, probe in PROBES.items():
        result = run(probe)
        row = result["row"]
        error = (row.get("error") or "").splitlines()
        blocked = row.get("outcome") == "crashed"
        if not blocked:
            reached.append(name)
        print(f"{name:<6} {result['exit']:>5} {row.get('outcome', '-'):<10} "
              f"{row.get('failure_kind', '-'):<28} "
              f"{error[0][:70] if error else '(the run completed)'}")
    print()
    print(f"isolation recorded on the row: "
          f"{json.dumps(result['row'].get('isolation', {}), sort_keys=True)}")
    print()
    if reached:
        print(f"REACHED THE NETWORK: {reached}. The split calendar is decorative "
              f"while this is true.")
        return 1
    print("all three refused, and each is a crashed trial with a row")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
