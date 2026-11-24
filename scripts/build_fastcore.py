#!/usr/bin/env python3
"""Build the native event core with the Zig C++ compiler.

    ~/.venvs/practicum-fast/bin/python scripts/build_fastcore.py

The library goes under `.scratch/native/`. It is a build product and is never committed.
`-ffp-contract=off` and the baseline CPU target keep floating-point results the same as the
Python reference, which is the condition for using this code at all.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "native" / "fastcore" / "fastcore.cpp"
OUTPUT = ROOT / ".scratch" / "native" / "libfastcore.so"


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "ziglang", "c++", "-std=c++17", "-O3", "-shared", "-fPIC",
               "-ffp-contract=off", "-fno-fast-math", "-target", "x86_64-linux-gnu.2.31",
               "-Wall", "-Wextra", "-Werror", "-o", str(OUTPUT), str(SOURCE)]
    subprocess.run(command, check=True, cwd=ROOT)
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
