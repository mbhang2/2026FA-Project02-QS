"""The worked examples in `docs/agent/strategy-api.md` run, and the doc matches.

A documentation example that does not run is worse than no example: the research
agent copies it verbatim and spends trials debugging someone else's typo. So the
examples are real files, they are executed here against the synthetic dataset,
and the code blocks in the document are asserted byte-identical to them.

Two separate failures, deliberately kept apart:

* an example stops working -> the *engine* changed under the documentation;
* an example and its code block differ -> someone edited one and not the other.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.types import Split  # noqa: E402

EXAMPLES_DIR = REPO_ROOT / "tests" / "examples"
DOC = REPO_ROOT / "docs" / "agent" / "strategy-api.md"

# Big enough that every example actually trades on it -- an example that runs
# without ever placing an order proves nothing.
SPEC = SyntheticSpec(
    n_sessions=24, ticks_per_session=390, vol_per_tick=0.15, seed=1,
    train_frac=0.5, validation_frac=0.2,
)


def example_paths() -> list[Path]:
    return sorted(p for p in EXAMPLES_DIR.glob("*.py") if not p.name.startswith("_"))


def load_strategy_class(path: Path):
    """Import the example by path, the way a reader would open the file."""
    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Strategy


def doc_blocks() -> dict[str, str]:
    """Fenced python blocks whose first line names a file under tests/examples/.

    The marker is a comment, so the block stays valid Python and can be copied
    straight out of the document.
    """
    out: dict[str, str] = {}
    pattern = re.compile(
        r"```python\n# (tests/examples/[^\n]+)\n(.*?)```", re.DOTALL
    )
    for name, body in pattern.findall(DOC.read_text()):
        out[name] = body.rstrip("\n")
    return out


def test_there_are_examples_to_check():
    """A guard on the guards: every test below passes vacuously if the glob or
    the regex stops matching."""
    paths = example_paths()
    assert len(paths) >= 5, [p.name for p in paths]
    assert len(doc_blocks()) >= 5, sorted(doc_blocks())


@pytest.mark.parametrize("path", example_paths(), ids=lambda p: p.stem)
def test_example_is_in_the_doc_byte_for_byte(path: Path):
    rel = str(path.relative_to(REPO_ROOT))
    blocks = doc_blocks()
    assert rel in blocks, (
        f"{rel} has no code block in docs/agent/strategy-api.md. Add one, opened "
        f"with `# {rel}` as its first line."
    )
    assert blocks[rel] == path.read_text().rstrip("\n"), (
        f"the code block for {rel} in docs/agent/strategy-api.md has drifted from "
        "the file. The file is the source of truth -- copy it back into the block."
    )


@pytest.mark.parametrize("path", example_paths(), ids=lambda p: p.stem)
def test_example_runs_and_trades(path: Path):
    strategy = load_strategy_class(path)()
    engine = Engine(
        SyntheticDataset(SPEC), strategy, split=Split.VALIDATION, warmup_sessions=1
    )
    record = engine.run()

    assert record.outcome == "completed", record.error
    assert record.fills > 0, (
        "the example ran but never traded, which proves nothing about the API"
    )
    assert record.actions_dropped == 0, (
        "the example hit the per-callback action cap; documentation should not "
        "demonstrate a ping-pong bug"
    )
    assert record.warmup_actions_ignored == 0, (
        "the example tried to act during warmup -- it is not checking "
        "ctx.actionable, and the doc tells readers to"
    )


@pytest.mark.parametrize("path", example_paths(), ids=lambda p: p.stem)
def test_example_is_deterministic(path: Path):
    """Same data, same strategy, same seed -> byte-identical blotter. If an
    example is not reproducible it is teaching the wrong thing."""
    digests = []
    for _ in range(2):
        engine = Engine(
            SyntheticDataset(SPEC), load_strategy_class(path)(),
            split=Split.VALIDATION, warmup_sessions=1,
        )
        engine.run()
        digests.append(engine.blotter.digest())
    assert digests[0] == digests[1]


def test_no_example_imports_the_scorer_or_the_metric():
    """The examples are what the agent copies. One of them reaching into
    `src.core.scoring` would not merely fail at import -- it would teach the
    agent that the boundary is negotiable."""
    for path in example_paths():
        text = path.read_text()
        for forbidden in ("src.core.scoring", "scorer", "metrics", "calibration"):
            assert forbidden not in text, f"{path.name} mentions {forbidden}"


# --------------------------------------------------------------------------- #
# the error catalogue in the document has to be the one the code raises
# --------------------------------------------------------------------------- #
# Each entry is the fixed part of a message the document quotes. Variable parts
# (ids, prices, sizes) are cut out. Both halves are checked: the fragment must
# appear in the source, so the doc cannot quote a message that no longer exists,
# and in the doc, so a message cannot be reworded without the doc following.
ERROR_FRAGMENTS = [
    "setup() declared no subscriptions",
    "setup() returns nothing now",
    "cannot read",
    "interval must be positive",
    "already subscribed with",
    "train_bars() aggregates; for raw train events use ctx.ticks()",
    "this context belonged to an earlier callback",
    "the setup context is only valid during setup()",
    "no subscription",
    "did not close in this callback",
    "subscriptions are not declared until setup() runs",
    "does not match the strategy contract",
    "(ctx) is missing",
    "is at or beyond the current instant",
    "reaches past the current instant",
    "reaches before the start of history",
    "size must be a positive whole number of contracts",
    "order needs limit=<price>",
    "order needs trigger=<price>",
    "a market order cannot carry a limit price",
    "give bracket levels either as prices",
    "must be below the entry",
    "must be above the entry",
    "is a positive distance from the fill price",
    "a GTD order needs expire_ts=<nanoseconds>",
    "close() with no position open",
    "cannot modify order",
    "modify() with nothing to change",
    "reduce_only with no position open",
    "would increase a position of",
    "size clamped from",
    "cancel for an order that is not working",
    "modify for an order that is not working",
    "cannot resize to",
    "abandoned after",
    "the holdout is not served until it is explicitly unlocked",
]


def source_text() -> str:
    return "\n".join(
        p.read_text() for p in sorted((REPO_ROOT / "src").rglob("*.py"))
    )


@pytest.mark.parametrize("fragment", ERROR_FRAGMENTS)
def test_documented_error_text_is_the_text_the_code_raises(fragment: str):
    assert fragment in source_text(), (
        f"docs/agent/strategy-api.md quotes {fragment!r} but nothing under src/ "
        "raises it any more"
    )
    assert fragment in DOC.read_text(), (
        f"src/ raises {fragment!r} but docs/agent/strategy-api.md no longer "
        "documents it"
    )
