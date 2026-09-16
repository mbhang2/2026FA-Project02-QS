"""Frames and message shapes for the isolation spike. Throwaway.

Length-prefixed frames over anonymous pipes, one request and one reply per
callback, synchronous. The engine blocks on the strategy's answer: there is no
concurrency to reason about and no scheduling nondeterminism, which is what makes
a byte-identical blotter a meaningful acceptance criterion rather than a lucky
interleaving.

**Direction matters for the codec.** The engine -> strategy direction may use
anything; the strategy -> engine direction is untrusted input, so the reply is
JSON and every action is validated against a whitelist before it reaches the
context. `pickle` in that direction is arbitrary code execution by design, and it
is exactly the shortcut a benchmark tempts you into. The engine -> strategy
direction uses JSON here too, so the spike measures the pessimistic case.
"""

from __future__ import annotations

import json
import os
import struct

HEADER = struct.Struct("<I")

# What a strategy is allowed to ask the engine to do. Anything else is a protocol
# error and kills the trial rather than being ignored -- a silently dropped
# action is a run that looks like a result.
ACTIONS = {
    "subscribe": {"interval", "emit_empty"},
    "buy": {"size", "type", "limit", "stop", "tif", "stop_offset", "target_offset",
            "tag", "reduce_only", "expire_ns"},
    "sell": {"size", "type", "limit", "stop", "tif", "stop_offset", "target_offset",
             "tag", "reduce_only", "expire_ns"},
    "close": {"tag"},
    "cancel": {"order_id"},
    "cancel_all": set(),
}


class ProtocolError(RuntimeError):
    """The strategy process sent something the engine will not act on."""


def send(fd: int, obj) -> None:
    blob = json.dumps(obj).encode()
    os.write(fd, HEADER.pack(len(blob)))
    written = 0
    while written < len(blob):
        written += os.write(fd, blob[written:])


def recv(fd: int):
    header = b""
    while len(header) < HEADER.size:
        chunk = os.read(fd, HEADER.size - len(header))
        if not chunk:
            raise EOFError("the peer closed the pipe")
        header += chunk
    want = HEADER.unpack(header)[0]
    out = bytearray()
    while len(out) < want:
        chunk = os.read(fd, want - len(out))
        if not chunk:
            raise EOFError("the peer closed the pipe mid-frame")
        out += chunk
    return json.loads(bytes(out))


def validate(actions) -> list[dict]:
    if not isinstance(actions, list):
        raise ProtocolError(f"actions must be a list, got {type(actions).__name__}")
    out = []
    for action in actions:
        if not isinstance(action, dict) or "op" not in action:
            raise ProtocolError(f"malformed action {action!r}")
        op = action["op"]
        if op not in ACTIONS:
            raise ProtocolError(f"unknown action {op!r}; allowed: {sorted(ACTIONS)}")
        extra = set(action) - {"op"} - ACTIONS[op]
        if extra:
            raise ProtocolError(f"{op} does not take {sorted(extra)}")
        out.append(action)
    return out


def bar_row(bar) -> list:
    return [bar.sub_id, bar.open_ts, bar.close_ts, bar.open, bar.high, bar.low,
            bar.close, bar.volume, bar.tick_count, bar.session]
