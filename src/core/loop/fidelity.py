"""Does the candidate do what its campaign's question says it must?

A hypothesis leaves most of a strategy to the agent and fixes a few things it
must not choose. Until this module nothing checked those few things before the
end of a campaign, and then only by one language model reading one champion. The
economics seat's record, read 2026-11-05 across 27 committees:

| campaign | what the champion did | what the question fixed |
|---|---|---|
| nq-flowcont-5m | held 55 s on average | "Positions are held at least 5 minutes" |
| nq-orderflow-absorption-02 | never built a volume profile | "It must condition on a level built from the volume profile" |
| nq-flowcont-01 | faded the flow | the flow continues |

Each of those campaigns spent its whole budget on a different question from the
one it registered, and learned so at the end. The first two are facts about the
code and the fills, not judgements, so they are checked here mechanically.

**The constraints file.** ``config/hypotheses/<slug>.constraints.yaml``, beside
the hypothesis it constrains, frozen by digest into the campaign record at
registration exactly as the hypothesis is. Every constraint quotes, in ``says``,
the sentence of the hypothesis it enforces, and ``load`` refuses one whose quote
is not in the hypothesis verbatim (whitespace-normalised). That is the grounding
rule applied to constraints: a constraint the question did not state is a new
question, and writing one after seeing results is the failure pre-registration
exists to prevent.

**Two kinds of check, at two times.**

* *Code* (``code_uses``, ``code_avoids``) reads the source's AST before the trial
  runs. Comments, docstrings and string literals are invisible to it, so a
  docstring that says "uses the volume profile" satisfies nothing; an identifier
  named only in an import does not count as use either. Executes nothing, so --
  like the static check -- its answer cannot depend on market data, and a
  violation may go back to the agent for repair without the repair counting as
  a trial.
* *Behaviour* (``hold_seconds``, ``entry_window``, ``flat_by``, ``no_overnight``,
  ``side``, ``max_position``) reads the trial's own fill log after it runs. The
  synthetic probe would have been the cleaner place, and was tried first: three
  of eight historical champions made no trades on it at all, so it cannot see
  behaviour. The fills are the train split's, so what reaches the agent from
  this half is a description of what its strategy did -- hold times, entry
  times -- never a score. It already sees trade and session counts from the same
  run.

**What a violation does is the campaign's registered ``policy``.**

* ``record`` -- written to ``fidelity.jsonl`` beside the ledger, shown to the
  agent in its history, nothing else.
* ``enforce`` -- as ``record``, and: a code violation the agent cannot repair in
  ``MAX_REPAIRS`` attempts is a blocked proposal; a behaviour violation stops the
  trial becoming champion; and the gate's observed best is taken over the
  on-question trials only.

**Why ``enforce`` cannot inflate the false-acceptance rate.** N is untouched:
every blocked or off-question trial is still a trial, its null sample is still
in ``max_of_n``, and the critical value is still the best of N nulls. Removing
candidates from the *observed* side can only lower the observed best. So the
gate's error rate under ``enforce`` is at most the one Task 57 measured; the cost
is power, in trials spent off the question.
"""
from __future__ import annotations

import ast
import datetime as dt
import gzip
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import yaml

SCHEMA = "constraints/1"
POLICIES = ("record", "enforce")
DEFAULT_CLOCK = "America/Chicago"
MAX_REPAIRS = 2

# **A behaviour constraint holds for 95% of round turns unless it says otherwise.**
# 100% was the first default and it was brittle in the way a rule about every
# fill is: on the historical record it failed nq-s04-01's champion for ONE entry
# in 375 outside its window, and the intraday families for 3% of round turns that
# ran through a session close. A question describes what a strategy does, not
# every fill it ever makes; a constraint that needs every fill writes `share: 1`.
# Chosen on the record and said so -- see docs/reviews/question-fidelity/.
DEFAULT_SHARE = 0.95

CODE_KINDS = ("code_uses", "code_avoids")
BEHAVIOUR_KINDS = ("hold_seconds", "entry_window", "flat_by", "no_overnight",
                   "side", "max_position")
KINDS = CODE_KINDS + BEHAVIOUR_KINDS

# The keys each kind takes, and which are required. A key the kind does not
# know is refused rather than ignored: a typo in `min` would otherwise be a
# constraint that silently checks nothing.
_KEYS = {
    "code_uses": ({"any_of"}, set()),
    "code_avoids": ({"none_of"}, set()),
    "hold_seconds": (set(), {"min", "max", "share"}),
    "entry_window": ({"start", "end"}, {"share"}),
    "flat_by": ({"time"}, {"share"}),
    "no_overnight": (set(), {"share"}),
    "side": ({"only"}, set()),
    "max_position": ({"value"}, set()),
}
_COMMON = {"id", "says", "check"}


class ConstraintsRejected(ValueError):
    """The constraints file is malformed or not grounded in its hypothesis."""


@dataclass(frozen=True, slots=True)
class Constraint:
    id: str
    says: str
    check: str
    params: dict

    @property
    def is_code(self) -> bool:
        return self.check in CODE_KINDS


@dataclass(frozen=True, slots=True)
class Violation:
    id: str
    check: str
    says: str
    detail: str

    def as_dict(self) -> dict:
        return {"id": self.id, "check": self.check, "says": self.says,
                "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Judged:
    """A requirement no mechanical check can express -- "not a fade", "it must
    condition on the opening range" -- left to the shadow reviewer
    (`scripts/fidelity_judge.py`). Grounded like a constraint: `says` is a quote."""
    id: str
    says: str


@dataclass(frozen=True)
class Constraints:
    path: str
    digest: str
    hypothesis: str
    clock: str
    items: tuple[Constraint, ...] = field(default_factory=tuple)
    judged: tuple[Judged, ...] = field(default_factory=tuple)

    @property
    def code(self) -> tuple[Constraint, ...]:
        return tuple(c for c in self.items if c.is_code)

    @property
    def behaviour(self) -> tuple[Constraint, ...]:
        return tuple(c for c in self.items if not c.is_code)


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def sidecar_for(hypothesis: Path) -> Path:
    """`x.md` -> `x.constraints.yaml`, beside it."""
    return hypothesis.with_name(hypothesis.stem + ".constraints.yaml")


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def load(path: Path, hypothesis_text: str) -> Constraints:
    """Read, validate, ground against the hypothesis, digest."""
    raw = path.read_bytes()
    try:
        doc = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConstraintsRejected(f"{path}: not YAML: {exc}") from exc
    if doc.get("schema") != SCHEMA:
        raise ConstraintsRejected(f"{path}: schema must be {SCHEMA!r}")
    clock = str(doc.get("clock") or DEFAULT_CLOCK)
    try:
        ZoneInfo(clock)
    except Exception as exc:                                   # noqa: BLE001
        raise ConstraintsRejected(f"{path}: unknown clock {clock!r}") from exc
    items = doc.get("constraints") or []
    judged_raw = doc.get("judged") or []
    if not items and not judged_raw:
        raise ConstraintsRejected(f"{path}: no constraints")
    hyp = _norm(hypothesis_text)
    seen: set[str] = set()
    out = []
    for i, c in enumerate(items):
        where = f"{path}: constraint {i}"
        if not isinstance(c, dict):
            raise ConstraintsRejected(f"{where} is not a mapping")
        cid, says, kind = c.get("id"), c.get("says"), c.get("check")
        if not cid or not isinstance(cid, str) or not re.fullmatch(r"[a-z0-9-]+", cid):
            raise ConstraintsRejected(f"{where}: id must be lowercase-kebab, got {cid!r}")
        if cid in seen:
            raise ConstraintsRejected(f"{where}: duplicate id {cid!r}")
        seen.add(cid)
        if kind not in KINDS:
            raise ConstraintsRejected(f"{where} ({cid}): unknown check {kind!r}; "
                                      f"known: {', '.join(KINDS)}")
        if not says or _norm(str(says)) not in hyp:
            raise ConstraintsRejected(
                f"{where} ({cid}): `says` must quote the hypothesis verbatim, and "
                f"{says!r} is not in it. A constraint the question did not state "
                "is a new question.")
        required, optional = _KEYS[kind]
        params = {k: v for k, v in c.items() if k not in _COMMON}
        missing = required - params.keys()
        unknown = params.keys() - required - optional
        if missing:
            raise ConstraintsRejected(f"{where} ({cid}): {kind} needs {sorted(missing)}")
        if unknown:
            raise ConstraintsRejected(f"{where} ({cid}): {kind} does not take "
                                      f"{sorted(unknown)}")
        _validate(kind, params, f"{where} ({cid})")
        out.append(Constraint(id=cid, says=str(says), check=kind, params=params))
    judged = []
    for i, j in enumerate(judged_raw):
        where = f"{path}: judged {i}"
        if not isinstance(j, dict) or set(j) != {"id", "says"}:
            raise ConstraintsRejected(f"{where}: takes exactly id and says")
        jid, says = j["id"], j["says"]
        if not isinstance(jid, str) or not re.fullmatch(r"[a-z0-9-]+", jid):
            raise ConstraintsRejected(f"{where}: id must be lowercase-kebab, got {jid!r}")
        if jid in seen:
            raise ConstraintsRejected(f"{where}: duplicate id {jid!r}")
        seen.add(jid)
        if not says or _norm(str(says)) not in hyp:
            raise ConstraintsRejected(
                f"{where} ({jid}): `says` must quote the hypothesis verbatim, and "
                f"{says!r} is not in it.")
        judged.append(Judged(id=jid, says=str(says)))
    return Constraints(path=str(path), digest=hashlib.sha256(raw).hexdigest(),
                       hypothesis=str(doc.get("hypothesis") or ""), clock=clock,
                       items=tuple(out), judged=tuple(judged))


def _hhmm(v, where: str) -> dt.time:
    try:
        return dt.time.fromisoformat(str(v))
    except ValueError as exc:
        raise ConstraintsRejected(f"{where}: {v!r} is not HH:MM") from exc


def _validate(kind: str, p: dict, where: str) -> None:
    share = p.get("share", DEFAULT_SHARE)
    if not (isinstance(share, (int, float)) and 0 < share <= 1):
        raise ConstraintsRejected(f"{where}: share must be in (0, 1]")
    if kind in CODE_KINDS:
        names = p.get("any_of") or p.get("none_of")
        if not isinstance(names, list) or not names or \
                not all(isinstance(n, str) and n.isidentifier() for n in names):
            raise ConstraintsRejected(f"{where}: needs a list of identifiers")
    elif kind == "hold_seconds":
        if "min" not in p and "max" not in p:
            raise ConstraintsRejected(f"{where}: hold_seconds needs min or max")
    elif kind == "entry_window":
        if _hhmm(p["start"], where) >= _hhmm(p["end"], where):
            raise ConstraintsRejected(f"{where}: start must be before end")
    elif kind == "flat_by":
        _hhmm(p["time"], where)
    elif kind == "side":
        if p["only"] not in ("long", "short"):
            raise ConstraintsRejected(f"{where}: side.only is long or short")
    elif kind == "max_position":
        if not (isinstance(p["value"], int) and p["value"] > 0):
            raise ConstraintsRejected(f"{where}: max_position.value is a positive int")


# --------------------------------------------------------------------------- #
# code
# --------------------------------------------------------------------------- #
def used_names(source: str) -> set[str]:
    """Identifiers the code USES: names and attribute names outside imports.

    The AST, not the text: comments, docstrings and string literals are not
    nodes it visits here, so describing a feature is not using it. Imports are
    excluded because `from x import volume_profile` and never calling it is the
    cheapest way to look compliant.
    """
    tree = ast.parse(source)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


def imported_names(source: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.update(a.name.split("."))
                if a.asname:
                    out.add(a.asname)
        elif isinstance(node, ast.ImportFrom):
            out.update((node.module or "").split("."))
            for a in node.names:
                out.add(a.name)
                if a.asname:
                    out.add(a.asname)
    return out


def code_violations(source: str, cons: Constraints) -> list[Violation]:
    if not cons.code:
        return []
    try:
        used = used_names(source)
        imported = imported_names(source)
    except SyntaxError:
        return []                       # the static check owns this failure
    out = []
    for c in cons.code:
        if c.check == "code_uses":
            want = c.params["any_of"]
            if not any(n in used for n in want):
                out.append(Violation(c.id, c.check, c.says,
                    f"the code never uses any of {', '.join(want)}"))
        else:
            bad = sorted(n for n in c.params["none_of"] if n in used or n in imported)
            if bad:
                out.append(Violation(c.id, c.check, c.says,
                    f"the code uses {', '.join(bad)}"))
    return out


# --------------------------------------------------------------------------- #
# behaviour
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RoundTurn:
    """Flat to flat. `quantity` is the opening fill's signed size."""
    entry_ts: int
    exit_ts: int
    entry_session: int
    exit_session: int
    quantity: int
    max_abs: int


def read_fill_rows(path: Path) -> list[list]:
    """A stored fill log (`runlog.store_fill_log`): one `FillRecord.as_row()`
    per line -- seq, ts, session, order_id, side, size, price, liquidity, tag."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def round_turns(rows: list) -> tuple[list[RoundTurn], int]:
    """Flat-to-flat round turns, and the largest absolute position.

    A position open at the end of the log is not a round turn and is not
    counted; its size still counts towards the maximum.
    """
    pos = 0
    worst = 0
    out: list[RoundTurn] = []
    open_ = None
    for r in sorted(rows, key=lambda r: (r[1], r[0])):
        ts, session, side, size = int(r[1]), int(r[2]), str(r[4]), int(r[5])
        signed = size if side.lower() == "buy" else -size
        before = pos
        pos += signed
        worst = max(worst, abs(pos))
        if before == 0 and pos != 0:
            open_ = [ts, session, pos, abs(pos)]
        elif open_ is not None:
            open_[3] = max(open_[3], abs(pos))
            if pos == 0:
                out.append(RoundTurn(open_[0], ts, open_[1], session,
                                     open_[2], open_[3]))
                open_ = None
    return out, worst


def _share(ok: int, n: int) -> float:
    return ok / n if n else 1.0


def behaviour_violations(rows: list, cons: Constraints) -> list[Violation]:
    if not cons.behaviour:
        return []
    turns, worst = round_turns(rows)
    tz = ZoneInfo(cons.clock)

    def local(ts: int) -> dt.datetime:
        return dt.datetime.fromtimestamp(ts / 1e9, tz)

    out = []
    for c in cons.behaviour:
        p = c.params
        need = float(p.get("share", DEFAULT_SHARE))
        n = len(turns)
        if c.check == "max_position":
            if worst > p["value"]:
                out.append(Violation(c.id, c.check, c.says,
                    f"the position reached {worst} contracts; the limit is {p['value']}"))
            continue
        if n == 0:
            continue                    # nothing traded, nothing to contradict
        if c.check == "hold_seconds":
            holds = [(t.exit_ts - t.entry_ts) / 1e9 for t in turns]
            lo, hi = p.get("min"), p.get("max")
            ok = sum(1 for h in holds if (lo is None or h >= lo)
                     and (hi is None or h <= hi))
            if _share(ok, n) < need:
                bound = (f"at least {lo:g} s" if hi is None else
                         f"at most {hi:g} s" if lo is None else
                         f"between {lo:g} and {hi:g} s")
                out.append(Violation(c.id, c.check, c.says,
                    f"{ok} of {n} round turns held {bound} (need {need:.0%}); "
                    f"median hold {median(holds):.0f} s"))
        elif c.check == "entry_window":
            a, b = dt.time.fromisoformat(p["start"]), dt.time.fromisoformat(p["end"])
            ok = sum(1 for t in turns if a <= local(t.entry_ts).time() <= b)
            if _share(ok, n) < need:
                out.append(Violation(c.id, c.check, c.says,
                    f"{n - ok} of {n} entries fell outside {p['start']}-{p['end']} "
                    f"{cons.clock} (need {need:.0%} inside)"))
        elif c.check == "flat_by":
            by = dt.time.fromisoformat(p["time"])

            def flat_in_time(t: RoundTurn) -> bool:
                if t.exit_session != t.entry_session:
                    return False
                x = local(t.exit_ts)
                # The CME session opens at 17:00 the evening before its date;
                # an exit in that evening segment is before any daytime cutoff.
                return x.time() <= by or x.time() >= dt.time(17, 0)
            ok = sum(1 for t in turns if flat_in_time(t))
            if _share(ok, n) < need:
                out.append(Violation(c.id, c.check, c.says,
                    f"{n - ok} of {n} round turns were still open after "
                    f"{p['time']} {cons.clock} (need {need:.0%} flat)"))
        elif c.check == "no_overnight":
            ok = sum(1 for t in turns if t.exit_session == t.entry_session)
            if _share(ok, n) < need:
                out.append(Violation(c.id, c.check, c.says,
                    f"{n - ok} of {n} round turns were carried across a session "
                    f"boundary (need {need:.0%} within one session)"))
        elif c.check == "side":
            wrong = sum(1 for t in turns if (t.quantity > 0) != (p["only"] == "long"))
            if wrong:
                out.append(Violation(c.id, c.check, c.says,
                    f"{wrong} of {n} round turns opened "
                    f"{'short' if p['only'] == 'long' else 'long'}"))
    return out


# --------------------------------------------------------------------------- #
# what the agent is told
# --------------------------------------------------------------------------- #
def repair_text(violations: list[Violation], source: str) -> str:
    lines = "\n".join(f"- [{v.id}] the question says: \"{v.says}\"\n"
                      f"  your file: {v.detail}" for v in violations)
    return (
        "Your previous file does not do what the campaign's question requires. "
        "This check reads the code's syntax tree and executes nothing; it reports "
        "no score and touches no market data. Comments and docstrings do not "
        "count -- only code that runs. Fix exactly this and return the whole "
        f"file again:\n\n{lines}\n\nPrevious file:\n\n```python\n{source}\n```")


def history_note(violations: list[dict]) -> str:
    return "OFF-QUESTION " + "; ".join(
        f"[{v['id']}] {v['detail']}" for v in violations)


# --------------------------------------------------------------------------- #
# the campaign's record of it
# --------------------------------------------------------------------------- #
LEDGER = "fidelity.jsonl"


def check_trial(row: dict, cons: Constraints, *, root: Path,
                code: list[dict] | None = None) -> list[dict]:
    """Every violation one trial has: the code ones the agent left unrepaired,
    and the behaviour ones in its stored fills.

    A trial with no stored fill log (it crashed, made no trades, or made more
    than `runlog.MAX_STORED_FILLS`) has no behaviour to contradict and is not
    charged with any.
    """
    out = list(code or [])
    src = row.get("fill_log_source")
    if cons.behaviour and src:
        f = Path(src)
        f = f if f.is_absolute() else root / f
        if f.is_file():
            out += [v.as_dict() for v in behaviour_violations(read_fill_rows(f), cons)]
    return out


def append_ledger(campaign_dir: Path, trial: int, violations: list[dict],
                  policy: str) -> None:
    """One line per trial, clean ones included, so "checked and clean" and
    "never checked" are different files rather than the same silence."""
    with (campaign_dir / LEDGER).open("a") as fh:
        fh.write(json.dumps({"trial": trial, "policy": policy,
                             "violations": violations}, sort_keys=True) + "\n")


def read_ledger(campaign_dir: Path) -> dict[int, dict]:
    f = campaign_dir / LEDGER
    if not f.exists():
        return {}
    out = {}
    for line in f.read_text().splitlines():
        if line.strip():
            note = json.loads(line)
            out[int(note["trial"])] = note
    return out


def excluded_trials(campaign_dir: Path, agent_config: dict | None) -> set[int]:
    """Trials the gate's observed best is NOT taken over: off-question ones,
    under a registered `enforce`. Empty under `record`, under no constraints,
    and for every campaign registered before this existed.

    Their null samples stay in `max_of_n` -- see the module docstring for why
    that makes the exclusion conservative rather than a free pass.
    """
    reg = (agent_config or {}).get("constraints")
    if not reg or reg.get("policy") != "enforce":
        return set()
    return {i for i, note in read_ledger(campaign_dir).items()
            if note.get("violations")}


# --------------------------------------------------------------------------- #
# the shadow reviewer's half that needs no model
# --------------------------------------------------------------------------- #
JUDGE_VERDICTS = ("HOLDS", "VIOLATED", "CANNOT_TELL")


def strip_for_review(source: str) -> str:
    """The code with every comment and docstring removed.

    A reviewer that reads "# this follows the flow, it does not fade" will
    believe it, and the proposer learns to write that sentence. `ast.unparse`
    drops comments for free; docstrings are removed from the tree first.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def parse_judgement(reply: dict, cons: Constraints, stripped: str) -> list[dict]:
    """One entry per judged requirement, in order, whatever the reply said.

    **A VIOLATED needs evidence that is in the code.** The reviewer must quote
    the line it objects to, and the quote must be a substring of the stripped
    source (whitespace-normalised). A violation it cannot point to is recorded
    as CANNOT_TELL: the same grounding rule the hypothesis and its constraints
    are held to, applied to the reviewer.
    """
    got = {str(x.get("id")): x for x in (reply.get("constraints") or [])
           if isinstance(x, dict)}
    body = " ".join(stripped.split())
    out = []
    for j in cons.judged:
        x = got.get(j.id) or {}
        verdict = str(x.get("verdict") or "").upper()
        evidence = str(x.get("evidence") or "")
        why = str(x.get("why") or "")
        if verdict not in JUDGE_VERDICTS:
            verdict, why = "CANNOT_TELL", f"no usable verdict ({x.get('verdict')!r})"
        if verdict == "VIOLATED" and (not evidence.strip()
                                      or " ".join(evidence.split()) not in body):
            verdict = "CANNOT_TELL"
            why = f"ungrounded: the quoted evidence is not in the code. {why}"
        out.append({"id": j.id, "says": j.says, "verdict": verdict,
                    "evidence": evidence, "why": why})
    return out


JUDGE_LEDGER = "fidelity-judge.jsonl"


def append_judgement(campaign_dir: Path, trial: int, result: dict) -> None:
    """Beside `fidelity.jsonl`, never in it: `excluded_trials` reads that file,
    and a shadow reviewer must not be able to move the gate by accident."""
    with (campaign_dir / JUDGE_LEDGER).open("a") as fh:
        fh.write(json.dumps({"trial": trial, **result}, sort_keys=True) + "\n")
