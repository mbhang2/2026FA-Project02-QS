"""The human's question, as a committed file, checked before it reaches a prompt.

A campaign investigates something, and until now there was nowhere to say what.
`config/prompts/strategy-agent.md` has slots for the interface, the current
strategy, the history and the run facts -- all of them facts about the harness,
none of them a research question. This module is the slot for the question.

**It lives in `config/`, not `docs/`.** `integrity.py` keeps `docs/` out of the
artifact set because "a result does not depend on prose". This prose *is* the
instruction the proposing agent works under, so two campaigns run under
different hypotheses are different experiments and their scores are not
comparable. That is the same argument `config/prompts/*.md` won in Task 36, and
it lands the same way: `config/hypotheses/*.md` is in `ARTIFACT_GLOBS`, and the
file's digest travels in the campaign record.

**The commit date is the pre-registration date**, exactly as for
`config/calendars/nq-trades.yaml`. Editing a hypothesis after seeing a
campaign's outcomes and re-running is a pre-registration written after the fact,
which is not one. Add a new file; the old one stays.

## Why there is a lint, and why it is a mechanism rather than a convention

`docs/design/loop.md` is exact about what may reach the agent: *"Nothing about
the gate"* -- no threshold, no p-value, no false-acceptance rate, no N, no
mention that a correction exists -- and *"Nothing about validation or holdout
reaches the prompt, ever."*

A hypothesis file is human free text flowing straight into that prompt. One
well-meant sentence -- "check it holds out of sample", "we want this significant
at 5%" -- breaks a guarantee the whole project rests on, and breaks it silently:
the campaign runs, the numbers look fine, and the separation between what the
agent optimizes and what the gate measures is gone with nothing in the record to
say so.

**An agent shown the gate optimizes the gate. An agent shown the score optimizes
the strategy.** The lint is what stops a human handing over the first one by
accident, and it is the reason this is a module rather than a docstring.

`BANNED` includes ordinary English -- "significant", "accept", "threshold" --
and will reject sentences that were innocent. That is the direction to err on. A
human who has to rephrase one line has lost a minute; a hypothesis that leaks the
gate into the prompt has cost the campaign, and nobody finds out.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# Terms that would tell the agent something about the gate, the correction, or a
# split it must never know exists. Matched case-insensitively on word
# boundaries, so "N" alone is not here -- it would reject every sentence -- and
# the phrases that actually carry the meaning are.
BANNED: tuple[tuple[str, str], ...] = (
    (r"hold[- ]?out", "names the holdout, which the agent must not know exists"),
    (r"out[- ]of[- ]sample", "names the holdout by its other name"),
    (r"validation", "names the validation split, which is closed"),
    (r"in[- ]sample", "implies a split the agent is not told about"),
    (r"p[- ]values?", "is the gate's output"),
    (r"significan(?:t|ce)", "is the gate's decision, in ordinary words"),
    (r"statistically", "reaches for the gate's vocabulary"),
    (r"false discovery", "is the correction"),
    (r"multiple testing", "is the correction"),
    (r"family[- ]wise", "is the correction"),
    (r"bonferroni|benjamini|hochberg|holm|westfall", "is a correction by name"),
    (r"deflat(?:ed|ion)", "is the deflated-Sharpe bar"),
    (r"\bfwer\b|\bfdr\b", "is the correction"),
    (r"\bq\s*=", "is the gate's level"),
    (r"alpha level|confidence level", "is the gate's level"),
    # Narrowed after measurement, not after argument. Bare "threshold" was in
    # this list and produced the only false positives found when the lint was
    # run over the hypothesis prose already in the repo: both hits in
    # `research/hypotheses/Test1.md` were "the paper's signal rule uses a
    # shifted zero threshold" -- a *signal* threshold, which is core vocabulary
    # for stating a trading rule and has nothing to do with the gate's bar. 31
    # of that file's 33 findings were true leaks; these 2 were the term's whole
    # contribution. A word a hypothesis must use to say what it means does not
    # belong in a banned list.
    (r"(?:significance|acceptance|rejection|decision)[- ]threshold",
     "is the gate's bar"),
    (r"\baccept(?:ed|s|ance)?\b", "is the gate's verdict"),
    (r"\breject(?:ed|s|ion)?\b", "is the gate's verdict"),
    (r"\b(?:\d+|n)\s+trials?\b|trial count|number of trials", "is N"),
    (r"correction", "is the correction"),
)

# Lines the lint does not read. A hypothesis file may need to say, in a comment
# to the next human, why a phrasing was chosen -- and an HTML comment is not in
# the rendered prompt, because the whole file is. It IS in the prompt. So this
# is deliberately empty and the constant exists to say that it was considered
# and rejected: every byte of the file reaches the agent, so every byte is
# linted.
EXEMPT_PREFIXES: tuple[str, ...] = ()


class HypothesisRejected(ValueError):
    """The file says something the agent must not be told."""


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """A linted hypothesis and its digest. Frozen: it is a pre-registration."""

    path: Path
    text: str
    digest: str

    @property
    def name(self) -> str:
        return self.path.stem


def findings(text: str) -> list[str]:
    """Every banned term in `text`, as readable lines. Empty means clean.

    Reports *all* of them rather than the first, so a human editing a hypothesis
    fixes one file once instead of playing whack-a-mole with a linter.
    """
    out: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern, why in BANNED:
            for m in re.finditer(pattern, line, flags=re.IGNORECASE):
                out.append(f"line {lineno}: {m.group(0)!r} -- {why}")
    return out


def lint(text: str, *, source: str = "<hypothesis>") -> None:
    """Raise `HypothesisRejected` naming every term, or return."""
    found = findings(text)
    if not found:
        return
    raise HypothesisRejected(
        f"{source} tells the agent about the gate or a closed split:\n  "
        + "\n  ".join(found)
        + "\n\nThe whole file is pasted into the prompt, so every line of it "
        "reaches the agent. Rephrase as a question about the market, not about "
        "how the answer will be judged. See src/core/loop/hypothesis.py for why "
        "this is enforced rather than advised."
    )


def load(path: Path) -> Hypothesis:
    """Read, lint, digest. The only way a hypothesis reaches an agent."""
    if not path.is_file():
        raise FileNotFoundError(
            f"no hypothesis at {path}. A campaign either states its question in "
            "config/hypotheses/ or runs without one -- there is no third option "
            "where the question lives somewhere unhashed."
        )
    text = path.read_text()
    if not text.strip():
        raise HypothesisRejected(
            f"{path} is empty. An empty hypothesis is not the same as no "
            "hypothesis: pass None for that, so the record says so.")
    lint(text, source=str(path))
    return Hypothesis(path=path, text=text,
                      digest=hashlib.sha256(text.encode()).hexdigest())


def block(hypothesis: "Hypothesis | None") -> str:
    """The prompt section, or the empty string.

    **Empty reproduces the pre-Task-46 messages byte for byte.** The template
    carries `{hypothesis}` alone on a line between `{run}` and the `---` that
    followed it, so substituting "" leaves exactly the blank line that was there
    before.

    The claim is about the two *assembled messages*, which is the precise and
    checkable version: `_call` splits the rendered template on `## USER` and
    then on `## SYSTEM`, so the HTML comment at the top of the file is never
    sent, and edits to that comment -- including the one that documents this
    slot -- cannot move what the agent saw. What did not move is what the model
    received.

    That is what keeps Task 36's and Task 37's campaigns comparable with a
    hypothesis-less campaign run today: the template file's digest changed, the
    messages did not. `tests/test_hypothesis.py` asserts it against the template
    as it stood at Task 45 rather than trusting this paragraph.
    """
    if hypothesis is None:
        return ""
    return (
        "\n### The question this campaign is investigating\n\n"
        "A human wrote this before the campaign was registered, and it is "
        "hashed into the campaign record. It is the direction of the search, "
        "not a specification to implement: propose strategies that investigate "
        "it, and use your judgement about how.\n\n"
        f"{hypothesis.text.strip()}\n"
    )
