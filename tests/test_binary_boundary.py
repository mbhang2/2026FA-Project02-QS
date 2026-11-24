"""The engine-to-strategy channel is exact, and the engine never decodes binary."""

from __future__ import annotations

import marshal
import os

import pytest

from src.core.engine.remote import protocol as p


def pipe_round_trip(obj):
    r, w = os.pipe()
    try:
        size = p.send_to_child(w, obj)
        return size, p.recv_from_parent(r)
    finally:
        os.close(r)
        os.close(w)


def test_a_message_crosses_exactly():
    message = {"kind": "on_bars", "scalars": {"ts": 2**62, "price": 100.1, "flag": True, "none": None},
               "closed": [[1, 2.5, "x"]], "windows": {"time:60": {"bars": [[1.25, -3]], "total": 7}}}
    size, got = pipe_round_trip(message)
    assert got == message and size > 0


def test_non_finite_values_survive():
    _, got = pipe_round_trip({"x": float("inf")})
    assert got["x"] == float("inf")


def test_a_value_marshal_refuses_still_crosses():
    class Text(str):
        pass

    _, got = pipe_round_trip({"x": Text("y")})
    assert got == {"x": "y"}


def test_the_engine_refuses_a_binary_frame_from_the_child():
    blob = b"M" + marshal.dumps({"reply": "done", "ok": True, "actions": []}, 4)
    r, w = os.pipe()
    try:
        os.write(w, p.HEADER.pack(len(blob)) + blob)
        with pytest.raises(ValueError):
            p.recv(r)
    finally:
        os.close(r)
        os.close(w)


def test_the_child_side_reply_is_json():
    r, w = os.pipe()
    try:
        p.send(w, {"reply": "done", "ok": True, "actions": []})
        assert p.recv(r)["reply"] == "done"
    finally:
        os.close(r)
        os.close(w)
