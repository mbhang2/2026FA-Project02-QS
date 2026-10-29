#!/usr/bin/env python3
"""Generate the remote adapter's view of the context surface.

    python scripts/gen_remote_surface.py            # writes the module
    python scripts/gen_remote_surface.py --check    # CI: is it current?

`BarContext` has 28 public names and the Task 24 spike implemented 12. It worked,
because the reference strategy reads 12 -- and `sub_id` reads like a scalar and
is a method, which cost a `TypeError` and would have cost a *silent wrong answer*
in a less lucky case. A hand-maintained list of 29 names is a list that will be
28 names in three tasks.

So the surface is generated from `src/core/engine/context.py`, the same source
`docs/design/context-api.md` is generated from, and every member is classified by
rule rather than by name:

- a `property` is **pushed**: scalar if its return annotation is a primitive,
  otherwise a collection needing a row encoder;
- a method annotated `-> None` or `-> int | None` is an **action**: it changes
  state and returns at most an order id;
- any other method is a **pull**: it returns data and takes arguments.

`subscribe()` is the one exception and is named as one: it queues an intent *and*
returns a `Subscription`. One documented exception beats 29 remembered names.

**Anything the rules cannot classify is an error, not an omission.** A new
context member breaks this generator with a message saying so, which is the whole
point -- the failure the spike hit was a member nobody had thought about.
"""

from __future__ import annotations

import argparse
import inspect
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.engine import context as ctxmod  # noqa: E402

OUT = REPO_ROOT / "src" / "core" / "engine" / "remote" / "_surface.py"

CLASSES = ("SetupContext", "BaseContext", "BarContext", "FillContext",
           "LifecycleContext", "TickContext", "QuoteContext")

# Annotations that need no encoder: they are JSON already.
PRIMITIVES = {"int", "float", "bool", "str", "Ts", "None"}

# The one member the rules get wrong, with the reason. Asserted to be exactly
# this set, so a second exception has to be argued for rather than appended.
EXCEPTIONS = {"subscribe": "action"}

# Types that cross the boundary and the function that turns one into JSON. A
# member whose annotation names a type absent from here fails the generator --
# that is how "the adapter cannot express this" surfaces at build time instead of
# as a wrong answer at run time.
ENCODERS = {
    "Bar": "bar_row",
    "TickEvent": "tick_row",
    "FillEvent": "fill_row",
    "LifecycleEvent": "lifecycle_row",
    "OrderView": "order_row",
    "Subscription": "subscription_row",
    "datetime": "datetime_row",
    "dt.datetime": "datetime_row",
    "_ClosedBars": "closed_bars_row",
    "Tick": "raw_tick_row",
    "QuoteEvent": "quote_row",
}


class Unclassifiable(RuntimeError):
    pass


def annotation_of(obj) -> str:
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):                       # pragma: no cover
        return ""
    ann = sig.return_annotation
    return "" if ann is inspect.Signature.empty else str(ann).strip("'\" ")


def element_type(annotation: str) -> str:
    """The type inside `tuple[X, ...]` / `HistoryView[X]`, or the type itself."""
    a = annotation.replace("'", "").strip()
    for opener in ("tuple[", "HistoryView["):
        if a.startswith(opener):
            return a[len(opener):].split(",")[0].rstrip("]").strip()
    return a


def classify(cls) -> dict:
    scalars, collections, pulls, actions, unknown = [], [], [], [], []
    for name, obj in sorted(vars(cls).items()):
        if name.startswith("_"):
            continue
        if name in EXCEPTIONS:
            (actions if EXCEPTIONS[name] == "action" else pulls).append(name)
            continue
        if isinstance(obj, property):
            ann = annotation_of(obj.fget)
            inner = element_type(ann)
            if not ann:
                unknown.append((name, "property with no return annotation"))
            elif inner in PRIMITIVES:
                scalars.append(name)
            elif inner in ENCODERS:
                collections.append((name, ENCODERS[inner]))
            else:
                unknown.append((name, f"property returns {ann!r}, no encoder"))
            continue
        if not callable(obj):
            unknown.append((name, f"neither property nor callable: {type(obj)}"))
            continue
        ann = annotation_of(obj)
        if ann in ("None", "int | None"):
            actions.append(name)
        elif ann:
            inner = element_type(ann)
            if inner in PRIMITIVES or inner in ENCODERS:
                pulls.append(name)
            else:
                unknown.append((name, f"method returns {ann!r}, no encoder"))
        else:
            unknown.append((name, "method with no return annotation"))
    return {"scalars": scalars, "collections": collections, "pulls": pulls,
            "actions": actions, "unknown": unknown}


def render() -> str:
    per_class, problems = {}, []
    for cname in CLASSES:
        result = classify(getattr(ctxmod, cname))
        per_class[cname] = result
        problems += [(cname, n, why) for n, why in result["unknown"]]
    if problems:
        raise Unclassifiable(
            "scripts/gen_remote_surface.py cannot classify:\n  "
            + "\n  ".join(f"{c}.{n}: {why}" for c, n, why in problems)
            + "\n\nAdd a return annotation, or an encoder in ENCODERS, or an "
              "entry in EXCEPTIONS with a reason. Do not leave it out: a context "
              "member the boundary does not carry is a silent wrong answer."
        )

    L = [
        '"""The context surface, classified. GENERATED -- do not edit.',
        "",
        "Written by `scripts/gen_remote_surface.py` from",
        "`src/core/engine/context.py`, the same source `docs/design/context-api.md`",
        "is generated from, so the boundary cannot drift from the interface it",
        "carries. Edit the context, then regenerate.",
        "",
        "`scalars` are pushed as-is. `collections` are pushed through the named",
        "encoder. `pulls` are methods the child asks for and the parent answers.",
        "`actions` are intents the child returns and the parent replays.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
    ]
    for cname in CLASSES:
        r = per_class[cname]
        L.append(f"{cname.upper()}_SCALARS = {tuple(r['scalars'])!r}")
        L.append(f"{cname.upper()}_COLLECTIONS = {tuple(r['collections'])!r}")
        L.append(f"{cname.upper()}_PULLS = {tuple(r['pulls'])!r}")
        L.append(f"{cname.upper()}_ACTIONS = {tuple(r['actions'])!r}")
        L.append("")
    L.append("SURFACE = {")
    for cname in CLASSES:
        r = per_class[cname]
        names = (r["scalars"] + [n for n, _ in r["collections"]]
                 + r["pulls"] + r["actions"])
        L.append(f"    {cname!r}: {tuple(sorted(names))!r},")
    L.append("}")
    L.append("")
    L.append(f"ENCODERS = {ENCODERS!r}")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    try:
        text = render()
    except Unclassifiable as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.check:
        current = OUT.read_text() if OUT.exists() else ""
        if current == text:
            print(f"{OUT.relative_to(REPO_ROOT)} is current")
            return 0
        print(f"{OUT.relative_to(REPO_ROOT)} is out of date; run "
              f"scripts/gen_remote_surface.py", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
