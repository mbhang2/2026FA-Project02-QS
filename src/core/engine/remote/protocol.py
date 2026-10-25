"""Frames, encoders and the action whitelist for the isolated strategy process.

Length-prefixed JSON frames over anonymous pipes. **JSON in both directions, and
never `pickle`**: unpickling a frame from the child is arbitrary code execution
by the process this whole boundary exists to distrust. It is 2.4x slower at 200
bars and the Task 24 projection was made at the slower number, so there is no
budget argument for changing it.

One request and one reply per callback. The engine blocks on the strategy: there
is no concurrency to reason about and no scheduling nondeterminism, which is what
makes a byte-identical blotter a meaningful test rather than a lucky interleaving.

`setup()` is the exception and serves pulls, because it happens once. Callbacks
do not, for the reason Task 24 measured: nothing bounds how many times a strategy
calls `ctx.history()` in a callback, the action cap bounds orders and there is no
read cap, and a cost whose multiplier is chosen by the untrusted side is not a
cost anyone can plan around.
"""

from __future__ import annotations

import json
import os
import struct

HEADER = struct.Struct("<I")

# `setup()` may pull training data. Bounded, because unbounded is 8.0 GB: the
# 384-session NQ training split as raw ticks. `train_bars("5m")` over the same
# split is 12.7 MB and `train_bars("1m")` is 62 MB, so the cap admits every bar
# interval a strategy would plausibly fit on and refuses the tick stream with a
# number rather than by hanging.
MAX_SETUP_BYTES = 256 * 1024 * 1024


class ProtocolError(RuntimeError):
    """The strategy process sent something the engine will not act on.

    Distinct from a strategy raising: a malformed frame is the strategy
    misusing the boundary, and the run record says so separately, because the
    two want different responses.
    """


class SetupPayloadTooLarge(RuntimeError):
    """A `setup()` pull would send more than `MAX_SETUP_BYTES`.

    Raised with the number, on the engine side, before anything is written to
    the pipe. `ctx.ticks()` on a real dataset lands here and that is the
    intended answer -- see `docs/design/architecture.md` on what it costs in
    expressiveness.
    """


# What a strategy may ask the engine to do, and with which keywords. Generated
# surfaces say which *methods* are actions; this says what their payload may
# contain, because a whitelist of names with unchecked keyword bags is not a
# whitelist.
ACTIONS = {
    "subscribe": {"interval", "emit_empty"},
    # **Keep this in step with `BaseContext._order`'s keyword-only parameters.**
    # It is a second copy of that signature, and on 2026-10-14 the two were
    # found to have drifted in both directions at once, costing 16 trials:
    #
    #   * `target` was accepted by `_order` and documented in
    #     `docs/agent/strategy-api.md` -- which is pasted into every proposal
    #     prompt, and which says absolute `stop`/`target` prices are "usually
    #     what you mean" -- but was missing here, so `sell does not take
    #     ['target']` fired 13 times. `stop` was allowed and `target` was not,
    #     an asymmetry nothing ever intended.
    #   * `expire_ns` was allowed here and does not exist: `_order` takes
    #     `expire_ts`, and so does the documentation, in four places including
    #     the error text `a GTD order needs expire_ts=<nanoseconds>`. So every
    #     GTD order the documentation asked for was rejected by the transport,
    #     3 times, and the key that was allowed could never be sent at all.
    #
    # In both cases the agent did exactly what it was told and the protocol
    # refused it. `test_remote.py` now asserts this set equals the signature,
    # so the next drift fails a test instead of a trial.
    # `asset` joined 2026-10-25 with the multi-asset work, and the test that
    # pins this set to the signature is what caught its absence -- which is
    # the drift above happening again and being stopped this time.
    "buy": {"size", "type", "limit", "stop", "target", "tif", "stop_offset",
            "target_offset", "tag", "reduce_only", "expire_ts", "trigger",
            "asset"},
    "sell": {"size", "type", "limit", "stop", "target", "tif", "stop_offset",
             "target_offset", "tag", "reduce_only", "expire_ts", "trigger",
             "asset"},
    "close": {"tag", "asset"},
    "cancel": {"order_id"},
    "cancel_all": set(),
    "modify": {"order_id", "limit", "trigger", "size"},
}


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


# --------------------------------------------------------------------------- #
# encoders -- one per type in `_surface.ENCODERS`, by that name
# --------------------------------------------------------------------------- #
# Generic, from `dataclasses.fields`, rather than one hand-written row function
# per type. Both sides import the same `src.core.types`, so the field order is
# the same object on both ends and cannot drift; a hand-written encoder is a
# second declaration of a shape that already exists, and the first draft of this
# file got four of the five field lists wrong.
#
# Enums cross as `str(value)` and come back through `Cls(value)`, which is exact
# for every enum in `types.py` -- asserted by a test rather than assumed, because
# a `StrEnum` whose `__str__` stopped matching its value would corrupt every side
# and status silently.
from dataclasses import fields as _fields  # noqa: E402
from enum import Enum  # noqa: E402


def encode(obj) -> list:
    out = []
    for field in _fields(obj):
        value = getattr(obj, field.name)
        out.append(str(value) if isinstance(value, Enum) else value)
    return out


def decode(cls, row: list):
    kwargs = {}
    for field, value in zip(_fields(cls), row):
        kwargs[field.name] = value
    return cls(**kwargs)


bar_row = encode
raw_tick_row = encode
tick_row = encode
fill_row = encode
lifecycle_row = encode
order_row = encode
subscription_row = encode
option_quote_row = encode


def datetime_row(d) -> str:
    return d.isoformat()


def closed_bars_row(_) -> None:
    """`ctx.bars` is a view over `ctx.closed`, not state of its own.

    Nothing is sent for it: the child rebuilds the same view from the `closed`
    list it already received, so the two cannot disagree. Present here because
    the generator requires an encoder for every type that crosses, and the
    honest encoder for a view is "this does not cross".
    """
    return None
