#!/usr/bin/env python3
"""Measurement 3: what one engine->strategy->engine round trip costs on a pipe.

    python docs/reviews/task24/measure_roundtrip.py

Length-prefixed frames over an anonymous pipe pair to a forked child, which is
what the prototype uses. The child echoes a reply of a stated size; the parent
times the full round trip. Payload sizes are the ones the callback measurement
says matter:

    124 B    a bar callback carrying one closed bar and the scalar context
    1.2 KB   ten bars
    24 KB    a 200-bar rolling window, the "push a window" shape
    2.4 MB   a 20,000-bar window -- the whole cumulative history at the end of a
             564-session NQ run, i.e. what a naive "serve the view" would send

Reported for two codecs. JSON is what a protocol would use if legibility mattered
more than speed; `pickle` is faster and is safe *in this direction* -- the engine
unpickles only what it sent the shape of, and the strategy process is the
untrusted side, so the engine must never unpickle a strategy's reply without a
whitelist. Stated here because "just use pickle" is the obvious move and it is
the one place in this design where obvious is dangerous.
"""

from __future__ import annotations

import json
import os
import pickle
import statistics
import struct
import sys
import time

HEADER = struct.Struct("<I")
TRIALS = 2000


def make_bar(i: int) -> list:
    return ["time:300000000000", 1_767_225_600_000_000_000 + i * 300_000_000_000,
            1_767_225_900_000_000_000 + i * 300_000_000_000,
            21_345.25, 21_352.75, 21_340.00, 21_349.50, 1_284, 96, 739_252]


def payload(n_bars: int) -> dict:
    return {
        "kind": "on_bars", "ts": 1_767_225_900_000_000_000, "seq": 4_812_993,
        "session": 739_252, "session_end": 1_767_243_600_000_000_000,
        "position": 1, "working_order_ids": [4, 5], "actionable": True,
        "bars": [make_bar(i) for i in range(n_bars)],
    }


def send(fd: int, blob: bytes) -> None:
    os.write(fd, HEADER.pack(len(blob)))
    written = 0
    while written < len(blob):
        written += os.write(fd, blob[written:])


def recv(fd: int) -> bytes:
    header = b""
    while len(header) < HEADER.size:
        chunk = os.read(fd, HEADER.size - len(header))
        if not chunk:
            raise EOFError
        header += chunk
    want = HEADER.unpack(header)[0]
    out = b""
    while len(out) < want:
        chunk = os.read(fd, want - len(out))
        if not chunk:
            raise EOFError
        out += chunk
    return out


def child(read_fd: int, write_fd: int, codec) -> None:
    """Decode the request, encode a small reply. What a strategy process does
    between messages is the strategy's own cost and is deliberately not here."""
    dumps, loads = codec
    reply = dumps({"actions": [{"op": "buy", "size": 1, "tag": "entry"}]})
    try:
        while True:
            loads(recv(read_fd))
            send(write_fd, reply)
    except (EOFError, OSError):
        pass
    os._exit(0)


def bench(n_bars: int, codec_name: str, codec) -> dict:
    dumps, loads = codec
    to_child_r, to_child_w = os.pipe()
    to_parent_r, to_parent_w = os.pipe()

    pid = os.fork()
    if pid == 0:
        os.close(to_child_w)
        os.close(to_parent_r)
        child(to_child_r, to_parent_w, codec)

    os.close(to_child_r)
    os.close(to_parent_w)

    blob = dumps(payload(n_bars))
    # warm the pipe and the allocator before timing
    for _ in range(50):
        send(to_child_w, blob)
        loads(recv(to_parent_r))

    samples = []
    for _ in range(TRIALS):
        t0 = time.perf_counter_ns()
        send(to_child_w, blob)
        loads(recv(to_parent_r))
        samples.append(time.perf_counter_ns() - t0)

    os.close(to_child_w)
    os.close(to_parent_r)
    os.waitpid(pid, 0)

    samples.sort()
    return {
        "bars": n_bars, "codec": codec_name, "bytes": len(blob),
        "median_us": samples[len(samples) // 2] / 1000,
        "p90_us": samples[int(len(samples) * 0.90)] / 1000,
        "mean_us": statistics.mean(samples) / 1000,
    }


def main() -> int:
    codecs = {
        "json": (lambda o: json.dumps(o).encode(), json.loads),
        "pickle": (lambda o: pickle.dumps(o, protocol=5), pickle.loads),
    }
    sizes = [1, 10, 200, 20_000]

    print(f"{TRIALS:,} round trips each, anonymous pipe, forked child, "
          f"length-prefixed frames")
    print(f"python {sys.version.split()[0]}  {os.uname().sysname} "
          f"{os.uname().machine}")
    print()
    print(f"{'bars':>7} {'codec':<8} {'payload':>10} {'median':>10} "
          f"{'p90':>10} {'mean':>10}")
    out = []
    for n in sizes:
        for name, codec in codecs.items():
            r = bench(n, name, codec)
            out.append(r)
            print(f"{r['bars']:>7,} {r['codec']:<8} {r['bytes']:>9,}B "
                  f"{r['median_us']:>9.1f}us {r['p90_us']:>9.1f}us "
                  f"{r['mean_us']:>9.1f}us")
    import pathlib
    (pathlib.Path(__file__).parent / "roundtrip.json").write_text(
        json.dumps(out, indent=2)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
