#!/usr/bin/env python3
"""Generate the context API reference the agent is handed.

    python scripts/gen_context_api.py            # writes docs/design/context-api.md
    python scripts/gen_context_api.py --check    # fails if it is out of date

Generated rather than written so it cannot drift from the code. One class with
docstrings is discoverable and autocompletes; this is the same information in the
form an agent reads before writing a strategy.
"""

from __future__ import annotations

import argparse
import inspect
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core import context as ctxmod  # noqa: E402

OUT = REPO_ROOT / "docs" / "design" / "context-api.md"

CLASSES = [
    (ctxmod.SetupContext, "`setup(ctx)` — once per run, before the walk"),
    (ctxmod.BaseContext, "Shared by every run-time callback"),
    (ctxmod.BarContext, "`on_bars(ctx)` — one or more subscribed bars closed"),
    (ctxmod.FillContext, "`on_fill(ctx)` — an order filled"),
    (ctxmod.LifecycleContext, "`on_lifecycle(ctx)` — accept, reject, expiry, session"),
]


def _sig(obj) -> str:
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):  # pragma: no cover
        return "(...)"


def _clean(doc: str | None) -> str:
    return inspect.cleandoc(doc or "").strip()


def members(cls):
    props, methods = [], []
    for name, obj in sorted(vars(cls).items()):
        if name.startswith("_"):
            continue
        if isinstance(obj, property):
            props.append((name, _clean(obj.__doc__)))
        elif callable(obj):
            methods.append((name, _sig(obj), _clean(obj.__doc__)))
    return props, methods


def render() -> str:
    L = ["# Context API reference", ""]
    L.append(
        "Everything a strategy can read or do. Generated from `src/core/context.py` "
        "by `scripts/gen_context_api.py` — edit the code, not this file."
    )
    L += ["", _clean(ctxmod.__doc__), ""]
    L.append("## Errors")
    L.append("")
    for exc in (ctxmod.ContextError, ctxmod.ContextExpired):
        L.append(f"**`{exc.__name__}`** — {_clean(exc.__doc__).splitlines()[0]}")
        rest = "\n".join(_clean(exc.__doc__).splitlines()[1:]).strip()
        if rest:
            L += ["", rest]
        L.append("")
    for cls, blurb in CLASSES:
        L.append(f"## `{cls.__name__}`")
        L += ["", f"*{blurb}*", ""]
        doc = _clean(cls.__doc__)
        if doc:
            L += [doc, ""]
        props, methods = members(cls)
        if props:
            L += ["### Reads", ""]
            for name, doc in props:
                first = doc.splitlines()[0] if doc else ""
                L.append(f"- **`ctx.{name}`** — {first}" if first else f"- **`ctx.{name}`**")
                for line in doc.splitlines()[1:]:
                    if line.strip():
                        L.append(f"  {line.strip()}")
            L.append("")
        if methods:
            L += ["### Calls", ""]
            for name, sig, doc in methods:
                sig = sig.replace("(self, ", "(").replace("(self)", "()")
                L.append(f"#### `ctx.{name}{sig}`")
                L += ["", doc or "_(no docstring)_", ""]
    L.append("---")
    L.append("")
    L.append(
        "Calls are queued: the context records the intent, the callback returns, and "
        "the engine applies the queue. Order methods return the id the order will "
        "have, or `None` if the action cap dropped it. A context is valid only "
        "during its own callback."
    )
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed reference is out of date")
    args = ap.parse_args()
    text = render()
    if args.check:
        current = OUT.read_text() if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(REPO_ROOT)} is out of date; "
                  "run scripts/gen_context_api.py", file=sys.stderr)
            return 1
        print("context API reference is current")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(REPO_ROOT)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
