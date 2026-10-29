"""The strategy runs in its own process, and the boundary is faithful.

Three things this file is for:

1. **The surface cannot drift.** `scripts/gen_remote_surface.py` classifies every
   public context member from `context.py`; these tests assert the child
   implements all of it, and that a member the generator cannot classify is a
   build failure rather than a silent omission. The Task 24 spike implemented 12
   of 28 names and worked, because the reference strategy reads 12.
2. **The boundary changes nothing.** Golden digests, in both paths, under every
   window configuration.
3. **The distinctions in the run record are real.** Five failure kinds, and the
   one that matters is `harness_failed`: a harness bug recorded as a crashed
   trial inflates N, and N is the denominator of every corrected p-value here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.engine import context as ctxmod  # noqa: E402
from src.core.engine.remote import (  # noqa: E402
    HarnessFailed, RemoteStrategy, StrategyRaised, StrategyViolatedProtocol,
    isolation_for,
)
from src.core.engine.remote import _surface, child as childmod  # noqa: E402
from src.core.engine.remote.protocol import (  # noqa: E402
    ACTIONS, ProtocolError, decode, encode,
)
from src.core.runlog import NotTheReferenceStrategy, RunTicket  # noqa: E402
from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.types import Split  # noqa: E402

REFERENCE = REPO_ROOT / "src" / "core" / "scoring" / "calibration" / "reference.py"
GOLDEN = json.loads(
    (REPO_ROOT / "tests" / "golden" / "pre_context_refactor.json").read_text())
IRREGULAR = json.loads(
    (REPO_ROOT / "tests" / "golden" / "irregular_generator.json").read_text())


def ds(**kw) -> SyntheticDataset:
    base = dict(n_sessions=200, ticks_per_session=390, vol_per_tick=0.15, seed=1)
    base.update(kw)
    return SyntheticDataset(SyntheticSpec(**base))


# --------------------------------------------------------------------------- #
# 1. the surface
# --------------------------------------------------------------------------- #
def test_the_generated_surface_is_current():
    r = subprocess.run(
        [sys.executable, "scripts/gen_remote_surface.py", "--check"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_surface_covers_every_public_context_member():
    """The generator's own claim, checked against the classes rather than against
    itself. If these disagree the generator has a rule that quietly skips."""
    for cname, names in _surface.SURFACE.items():
        cls = getattr(ctxmod, cname)
        public = {n for n in vars(cls) if not n.startswith("_")}
        assert public == set(names), (
            f"{cname}: generated surface {sorted(names)} != "
            f"public members {sorted(public)}"
        )


@pytest.mark.parametrize("name", sorted(
    set(_surface.SURFACE["BaseContext"]) | set(_surface.SURFACE["FillContext"])
    | set(_surface.SURFACE["LifecycleContext"]) | set(_surface.SURFACE["TickContext"])
    | set(_surface.SURFACE["QuoteContext"])
))
def test_the_child_implements_every_run_time_member(name):
    """The failure the spike hit, as a test. `sub_id` reads like a scalar and is
    a method; implementing 12 of 28 worked only because the reference strategy
    reads 12."""
    assert hasattr(childmod.RemoteContext, name) or name in (
        childmod.RemoteContext.__dict__
    ), f"RemoteContext does not implement ctx.{name}"


@pytest.mark.parametrize("name", sorted(_surface.SURFACE["SetupContext"]))
def test_the_child_implements_every_setup_member(name):
    assert hasattr(childmod.SetupContext, name), (
        f"the child's SetupContext does not implement ctx.{name}"
    )


def test_an_unclassifiable_member_fails_the_generator(monkeypatch):
    """The property the generator exists for: a new context member breaks the
    build with a message, rather than being carried as `AttributeError` on the
    far side halfway through a campaign."""
    import scripts.gen_remote_surface as gen

    class Odd:
        @property
        def mystery(self):          # no return annotation
            return object()

    with pytest.raises(gen.Unclassifiable, match="mystery"):
        monkeypatch.setattr(gen, "CLASSES", ("Odd",))
        monkeypatch.setattr(gen.ctxmod, "Odd", Odd, raising=False)
        gen.render()


def test_every_action_the_surface_names_has_a_whitelist():
    """A whitelist of method names with unchecked keyword bags is not a
    whitelist."""
    for name in _surface.BASECONTEXT_ACTIONS + _surface.SETUPCONTEXT_ACTIONS:
        assert name in ACTIONS, f"{name} is an action with no allowed-keyword set"


def test_enum_values_round_trip_through_their_string_form():
    """Every encoder sends `str(value)` and every decoder calls `Cls(value)`. A
    `StrEnum` whose `__str__` stopped matching its value would corrupt every side
    and status on the wire, silently."""
    from src.core.types import (
        LifecycleKind, OrderStatus, OrderType, Side, TimeInForce,
    )
    for enum in (Side, OrderType, TimeInForce, OrderStatus, LifecycleKind):
        for member in enum:
            assert enum(str(member)) is member, (enum.__name__, member)


def test_value_types_survive_a_real_json_round_trip():
    """JSON has no tuples. A field that was a tuple has to come back as one, or a
    decoded event compares unequal to the one that was sent."""
    from src.core.types import Bar, FillEvent, Side

    bar = Bar(sub_id="time:300000000000", open_ts=1, close_ts=2, open=1.0,
              high=2.0, low=0.5, close=1.5, volume=3, tick_count=4,
              session=739000)
    fill = FillEvent(ts=1, seq=2, order_id=3, side=Side.BUY, price=1.0, size=1,
                     position=1, last_trade_price=1.0, working_order_ids=(4, 5))
    for value, decoder in ((bar, childmod._decode_bar),
                           (fill, childmod._decode_fill)):
        assert decoder(json.loads(json.dumps(encode(value)))) == value


# --------------------------------------------------------------------------- #
# 2. the boundary changes nothing
# --------------------------------------------------------------------------- #
def _both_paths(spec: dict, fill_model: str, *, window: int = 512,
                warmup: int = 1, strategy=None, cls: str = "ReferenceStrategy",
                path: Path = REFERENCE):
    inproc = Engine.for_test(
        SyntheticDataset(SyntheticSpec(**spec)), (strategy or ReferenceStrategy)(),
        split=Split.VALIDATION, warmup_sessions=warmup, fill_model=fill_model,
        seed=spec.get("seed", 1),
    )
    inproc.run()
    with RemoteStrategy(path, cls, window=window, sandbox=False) as remote:
        isolated = Engine.for_test(
            SyntheticDataset(SyntheticSpec(**spec)), remote,
            split=Split.VALIDATION, warmup_sessions=warmup,
            fill_model=fill_model, seed=spec.get("seed", 1),
        )
        isolated.run()
    return inproc, isolated


@pytest.mark.parametrize("window", [1, 8, 512])
def test_the_isolated_path_reproduces_the_in_process_blotter(window):
    """The acceptance criterion, at three window sizes. The reference strategy
    reads one bar, so a window of 1 is enough and the larger ones must not
    change anything -- a window that changes the answer is a window that is being
    read past."""
    inproc, isolated = _both_paths(
        dict(n_sessions=200, ticks_per_session=390, vol_per_tick=0.15, seed=1),
        "unfavorable", window=window,
    )
    assert isolated.blotter.digest() == inproc.blotter.digest()
    assert isolated.fill_log_digest() == inproc.fill_log_digest()
    assert isolated.reconstructed().digest() == inproc.reconstructed().digest()
    assert len(inproc.blotter.rows) > 100, "the run traded too little to prove much"


@pytest.mark.parametrize(
    "case", GOLDEN["cases"],
    ids=lambda c: f"{c['fill_model']}-s{c['spec']['signal_strength']}-seed{c['spec']['seed']}",
)
def test_the_uniform_golden_set_holds_through_the_boundary(case):
    """The digests are the same object on both paths, so a digest that moves in
    one is behaviour that moved. `MomentumStrategy` is the agent's committed
    file, which is what the golden set was recorded against."""
    inproc, isolated = _both_paths(
        case["spec"], case["fill_model"],
        strategy=_momentum(), cls="MomentumStrategy",
        path=REPO_ROOT / "src" / "strategies" / "current.py",
    )
    assert inproc.blotter.digest() == case["blotter_digest"]
    assert isolated.blotter.digest() == case["blotter_digest"]
    assert len(isolated.blotter.rows) == case["rows"]


@pytest.mark.parametrize(
    "case", IRREGULAR["cases"],
    ids=lambda c: f"{c['fill_model']}-s{c['spec']['signal_strength']}-seed{c['spec']['seed']}",
)
def test_the_irregular_golden_set_holds_through_the_boundary(case):
    """The half that finds things: Task 17 surfaced four defects on the irregular
    generator and none on the uniform one."""
    inproc, isolated = _both_paths(
        case["spec"], case["fill_model"],
        strategy=_momentum(), cls="MomentumStrategy",
        path=REPO_ROOT / "src" / "strategies" / "current.py",
    )
    assert inproc.blotter.digest() == case["blotter_digest"]
    assert isolated.blotter.digest() == case["blotter_digest"]


def _momentum():
    from src.strategies.current import MomentumStrategy
    return MomentumStrategy


# --------------------------------------------------------------------------- #
# 3. in process only for the reference
# --------------------------------------------------------------------------- #
def test_only_the_reference_strategy_may_run_in_process():
    """Findings 2 and 3 are both 'strategy code and engine code share an address
    space'. A single in-process exception for arbitrary code puts them back, so
    the exception is bound to a digest rather than to an intention."""
    assert RunTicket.for_calibration("null tabulation").kind == "calibration"
    with pytest.raises(NotTheReferenceStrategy, match="not the committed"):
        RunTicket.for_calibration(
            "sneaking the agent's file in",
            REPO_ROOT / "src" / "strategies" / "current.py",
        )


def test_the_scored_path_never_imports_the_agents_file():
    """`run.load_strategy()` was the last place on the scored path that put
    agent code in the harness's address space."""
    import ast

    tree = ast.parse((REPO_ROOT / "run.py").read_text())
    calls = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "load_strategy"
    ]
    assert not calls, f"run.py still imports the strategy in process at {calls}"


# --------------------------------------------------------------------------- #
# 4. the error taxonomy
# --------------------------------------------------------------------------- #
def _run_hostile(tmp_path, body: str, **kw):
    path = tmp_path / "hostile.py"
    path.write_text(
        "class Strategy:\n"
        "    def setup(self, ctx): ctx.subscribe('5m')\n"
        f"{body}"
        "    def on_fill(self, ctx): pass\n"
        "    def on_lifecycle(self, ctx): pass\n"
    )
    with RemoteStrategy(path, "Strategy", sandbox=False, **kw) as remote:
        engine = Engine.for_test(ds(n_sessions=40, ticks_per_session=120),
                                 remote, split=Split.VALIDATION,
                                 warmup_sessions=1, seed=1)
        engine.run()


def test_a_strategy_that_raises_arrives_with_its_traceback(tmp_path):
    """If the traceback does not survive, every error message in
    `docs/agent/strategy-api.md` becomes a lie and the agent debugs fiction."""
    with pytest.raises(StrategyRaised) as caught:
        _run_hostile(tmp_path,
                     "    def on_bars(self, ctx):\n"
                     "        raise ValueError('deliberate, from the child')\n")
    assert "deliberate, from the child" in str(caught.value)
    tb = caught.value.child_traceback
    assert "ValueError" in tb and "on_bars" in tb and "hostile.py" in tb
    assert caught.value.kind == "strategy_raised"


def test_a_strategy_that_sends_a_bad_action_is_a_protocol_violation(tmp_path):
    """Distinct from raising: the strategy misused the boundary rather than
    failing inside it, and the two want different responses."""
    from src.core.engine.remote.adapter import validate_actions

    with pytest.raises(ProtocolError, match="unknown action"):
        validate_actions([{"op": "delete_the_ledger"}])
    with pytest.raises(ProtocolError, match="does not take"):
        validate_actions([{"op": "buy", "size": 1, "unlock_holdout": True}])
    with pytest.raises(ProtocolError, match="must be a list"):
        validate_actions({"op": "buy"})


def test_the_failure_kinds_are_distinguishable_and_harness_failure_is_separate():
    """The distinction that matters. A harness bug recorded as a crashed trial
    inflates N and quietly changes every corrected p-value."""
    from src.core.engine.remote import adapter

    kinds = {
        adapter.StrategyRaised: "strategy_raised",
        adapter.StrategyTimedOut: "strategy_timed_out",
        adapter.StrategyExceededResources: "strategy_exceeded_resources",
        adapter.StrategyViolatedProtocol: "strategy_violated_protocol",
        adapter.HarnessFailed: "harness_failed",
    }
    assert len(set(kinds.values())) == 5
    for cls, kind in kinds.items():
        assert cls.kind == kind
        assert issubclass(cls, adapter.RemoteFailure)
    # and only the last is not a trial
    assert adapter.HarnessFailed.kind == "harness_failed"


def test_a_pre_execution_sysexit_is_a_harness_failure_not_a_strategy_raise():
    """No callback and no traceback means the strategy never got control."""
    from types import SimpleNamespace

    from src.core.engine.remote import adapter

    remote = adapter.RemoteStrategy(Path("strategy.py"))
    remote._proc = SimpleNamespace(poll=lambda: 71)
    failure = remote._died()
    assert isinstance(failure, adapter.HarnessFailed)
    assert not isinstance(failure, adapter.StrategyRaised)
    assert failure.kind == "harness_failed"
    assert failure.child_exit_status == 71
    assert failure.completed_callbacks == 0


def test_a_strategy_that_reaches_past_the_window_is_told_so(tmp_path):
    """The one semantic difference between the two paths, and it is loud."""
    with pytest.raises(StrategyRaised, match="outside the pushed window"):
        _run_hostile(tmp_path,
                     "    def on_bars(self, ctx):\n"
                     "        h = ctx.history('5m')\n"
                     "        if len(h) > 3: h[0]\n",
                     window=2)


# --------------------------------------------------------------------------- #
# 5. isolation is reported, not assumed
# --------------------------------------------------------------------------- #
def test_isolation_is_described_per_platform_not_claimed_once():
    linux = isolation_for("Linux", mechanism="bubblewrap: --ro-bind / /")
    weaker = isolation_for("Linux", mechanism="unshare: user+mount+net")
    # `mechanism="none"` rather than omitting it: on a Linux host `isolation_for`
    # probes for the tool and would report what that machine actually has, which
    # is the right behaviour and the wrong thing for this assertion. Omitting the
    # argument made this test pass on macOS and fail on Linux -- a test that
    # measures the machine instead of the code.
    bare = isolation_for("Linux", mechanism="none")
    mac = isolation_for("Darwin")

    assert linux.level == "isolated"
    assert weaker.level == "best-effort", (
        "unshare + a remount is weaker than one syscall and must not claim "
        "the same level as bubblewrap"
    )
    assert bare.level == "none", "no sandbox tool is not isolation"
    assert mac.level == "best-effort"
    assert "cannot be set" in mac.memory_ceiling, (
        "macOS cannot set RLIMIT_AS/DATA/RSS; the record must say so"
    )
    assert isolation_for("Plan9").level == "none"
    for iso in (linux, weaker, bare, mac):
        assert set(iso.as_dict()) == {
            "level", "platform", "detail", "filesystem", "network",
            "address_space", "cpu", "wall_clock"
        }


def test_the_isolation_record_says_active_per_restriction_not_prose():
    """A row should be filterable on `isolation.network.active` without parsing
    English, and still say by what mechanism -- "denied" with no mechanism is a
    claim that stays true after someone edits a config file."""
    mac = isolation_for("Darwin").as_dict()
    assert mac["network"]["active"] is True
    assert mac["address_space"]["active"] is False, (
        "macOS has no memory ceiling and the row must not imply one"
    )
    assert mac["filesystem"]["mechanism"].startswith("sandbox-exec")

    off = isolation_for("Darwin", disabled=True).as_dict()
    assert off["level"] == "none"
    assert not off["network"]["active"] and not off["filesystem"]["active"]
    assert "not evidence" in off["detail"]


def test_every_run_record_says_which_isolation_it_had(tmp_path):
    """A row that says `best-effort` is a row a reader can discount. A row that
    does not say lies by omission."""
    led = tmp_path / "ledger.jsonl"
    r = subprocess.run(
        [sys.executable, "run.py", "--human", "--quiet", "--ledger", str(led)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stderr
    row = json.loads(led.read_text().splitlines()[0])
    assert row["isolation"]["level"] in ("isolated", "best-effort", "none")
    assert row["isolation"] == isolation_for().as_dict()


# --------------------------------------------------------------------------- #
# 6. the reachability walk, re-run inside the child
# --------------------------------------------------------------------------- #
# `tests/test_boundary.py` walks the object graph from every context on the
# in-process path and finds one live escape: `gc.get_referents` goes straight
# through `_SealedDataset.__getattr__` to the raw dataset. That is second-pass
# finding 2 and it is why `KNOWN_LIVE_ESCAPES` has an entry.
#
# The same walk, run in the strategy's own process, is the claim that isolation
# closes it structurally rather than by patch: there is no dataset in the address
# space to forward. Run in the child, because a walk run in the parent would be
# walking the wrong process.
WALK = r'''
import gc, importlib.util, json, os, sys
sys.path.insert(0, %r)


def _by_path(name, path):
    """Exactly how `adapter.py` starts the child: by path, never by package.

    Importing `src.core.engine.remote.child` here instead would execute
    `src/core/engine/__init__.py` in *this* process and put the whole simulator
    in the module list -- measuring the test's own imports rather than the
    child's.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_here = os.path.join(%r, "src", "core", "engine", "remote")
_protocol = _by_path("_remote_protocol", os.path.join(_here, "protocol.py"))
_child = _by_path("_remote_child", os.path.join(_here, "child.py"))
recv, send = _protocol.recv, _protocol.send
RemoteContext, SetupContext, load = (_child.RemoteContext, _child.SetupContext,
                                     _child.load)

read_fd, write_fd = int(os.environ["REMOTE_READ_FD"]), int(os.environ["REMOTE_WRITE_FD"])
strategy = load(%r, "Strategy")
actions, state = [], {"bars": {}, "ticks": {"items": [], "total": 0},
                      "sub_ids": {}, "ids": None}
setup = SetupContext({"dataset_name": "synthetic", "declared": []}, actions,
                     lambda *a: [])
strategy.setup(setup)
ctx = RemoteContext({"kind": "on_bars", "scalars": {}, "windows": {},
                     "closed": [], "fills": [], "working_orders": [],
                     "now": "1970-01-01T00:00:00"}, actions, state)

FORBIDDEN = ("Engine", "SyntheticDataset", "NQDataset", "Blotter", "HoldoutLock",
             "_SealedDataset", "_RunState", "_OrderBook", "FillRecord",
             "IntentQueue", "_SubResolver")
seen, stack, hits = set(), [("strategy", strategy), ("ctx", ctx),
                            ("setup", setup)], []
while stack and len(seen) < 400000:
    path, obj = stack.pop()
    if id(obj) in seen:
        continue
    seen.add(id(obj))
    if type(obj).__name__ in FORBIDDEN:
        hits.append(path + " -> " + type(obj).__name__)
        continue
    if isinstance(obj, (str, bytes, int, float, bool, type(None))):
        continue
    if type(obj).__name__ in ("module", "code"):
        continue
    for ref in gc.get_referents(obj):
        stack.append((path + ".<ref " + type(ref).__name__ + ">", ref))

send(write_fd, {"reply": "done", "ok": True, "actions": [],
                "hits": hits, "walked": len(seen),
                "src_modules": sorted(m for m in sys.modules
                                      if m.startswith("src.")),
                "fds": sorted(int(f) for f in os.listdir("/dev/fd") if f.isdigit())})
'''


def test_nothing_in_the_child_reaches_an_engine_or_a_dataset(tmp_path):
    """Finding 2, closed structurally. Not patched -- absent.

    Run with the agent's file, which is the strategy isolation exists for. The
    *calibration reference* is a different case and deliberately so: it lives in
    `src/core/scoring/calibration/`, so importing it executes that package's
    `__init__` and pulls the engine in with it. It also runs in process by
    design -- `RunTicket.for_calibration` is the only in-process route and it
    checks the digest -- so isolating it is not a case that arises. Noted here
    because the module list below would otherwise look like a stronger claim
    than it is.
    """
    import os

    from src.core.engine.remote.protocol import recv

    script = tmp_path / "walk.py"
    script.write_text(WALK % (
        str(REPO_ROOT), str(REPO_ROOT),
        str(REPO_ROOT / "src" / "strategies" / "current.py")))

    to_child_r, to_child_w = os.pipe()
    to_parent_r, to_parent_w = os.pipe()
    os.set_inheritable(to_child_r, True)
    os.set_inheritable(to_parent_w, True)
    proc = subprocess.Popen(
        [sys.executable, str(script)], close_fds=False, cwd=str(REPO_ROOT),
        env=dict(os.environ, REMOTE_READ_FD=str(to_child_r),
                 REMOTE_WRITE_FD=str(to_parent_w)),
    )
    os.close(to_child_r)
    os.close(to_parent_w)
    reply = recv(to_parent_r)
    os.close(to_child_w)
    os.close(to_parent_r)
    proc.wait(timeout=30)

    assert reply["walked"] > 1000, "the walk stopped too early to prove anything"
    assert not reply["hits"], reply["hits"]
    # and the only harness code in the child is value types
    assert set(reply["src_modules"]) <= {
        "src", "src.core", "src.core.types", "src.core.engine",
        "src.core.engine.remote", "src.core.engine.remote.child",
        "src.core.engine.remote.protocol", "src.core.engine.remote._surface",
        "src.strategies", "src.strategies._remote_strategy",
    }, sorted(reply["src_modules"])
    for forbidden in ("src.core.engine.engine", "src.core.data",
                      "src.core.scoring.scorer", "src.core.scoring.metrics"):
        assert forbidden not in reply["src_modules"], forbidden


# --------------------------------------------------------------------------- #
# 7. network egress -- the hole neither adversarial pass looked at
# --------------------------------------------------------------------------- #
# A strategy that fetches real NQ prices for the evaluation dates from any public
# source, and trades on them, produces a spectacular result with a genuine
# blotter, a stable dataset digest, a clean scoring fingerprint and a replay that
# reproduces -- because the replay fetches them again. It never touches the
# engine, the dataset or the scorer, so every defence in Tasks 21 through 25 is
# orthogonal to it.
#
# Both passes reasoned about the object graph, because both briefs framed the
# boundary as an in-process one. The reachability walk is a good tool that
# answers exactly one question.
#
# This is a **data** boundary, not only a process boundary. The whole point of
# the pre-registered split is that a strategy cannot see certain prices, and an
# open socket makes the calendar decorative.
NETWORK_PROBES = {
    "dns": "socket.getaddrinfo('example.com', 80)",
    "tcp": "socket.create_connection(('1.1.1.1', 80), timeout=5).close()",
    "http": "urllib.request.urlopen('http://1.1.1.1/', timeout=5).read(16)",
}


def _network_strategy(tmp_path, probe: str) -> Path:
    path = tmp_path / "netprobe.py"
    path.write_text(
        "import socket, urllib.request\n"
        "class Strategy:\n"
        "    def setup(self, ctx): ctx.subscribe('5m')\n"
        "    def on_bars(self, ctx):\n"
        f"        {probe}\n"
        "    def on_fill(self, ctx): pass\n"
        "    def on_lifecycle(self, ctx): pass\n"
    )
    return path


@pytest.mark.parametrize("name", sorted(NETWORK_PROBES))
def test_the_strategy_process_cannot_reach_the_network(name, tmp_path):
    """Three probes, because they fail in different places: the resolver, the
    connect, and the library a strategy would actually use. A configuration that
    denies one and not the others is worse than none, because it reads as
    protection."""
    from src.core.engine.remote import isolation_for

    if not isolation_for().as_dict()["network"]["active"]:
        pytest.skip("this platform does not claim network denial")

    path = _network_strategy(tmp_path, NETWORK_PROBES[name])
    with pytest.raises(StrategyRaised) as caught:
        with RemoteStrategy(path, "Strategy") as remote:
            Engine.for_test(ds(n_sessions=40, ticks_per_session=120), remote,
                            split=Split.VALIDATION, warmup_sessions=1,
                            seed=1).run()
    message = str(caught.value)
    assert any(sign in message for sign in (
        "Operation not permitted",       # macOS sandbox-exec
        "Network is unreachable",        # Linux empty netns
        "nodename nor servname",         # macOS resolver with no route
        "Temporary failure in name resolution",   # Linux resolver, same
    )), message
    assert caught.value.kind == "strategy_raised"


def test_network_denial_is_recorded_on_the_row_not_remembered():
    """A row produced without network denial is a row a reader should discount,
    and it has to say so itself rather than relying on someone remembering which
    week it was run."""
    from src.core.engine.remote import isolation_for

    for system in ("Linux", "Darwin"):
        block = isolation_for(system).as_dict()
        assert "network" in block and set(block["network"]) == {"active", "mechanism"}
    off = isolation_for(disabled=True).as_dict()
    assert off["network"]["active"] is False
    assert off["network"]["mechanism"] == "unrestricted"


def test_nothing_legitimate_in_the_child_needs_the_network():
    """Checked rather than assumed. The child receives everything over the pipe;
    if some part of it reached out, denying network would break a real run and
    that would be a finding in itself."""
    import ast

    for name in ("child.py", "protocol.py", "_surface.py"):
        source = (REPO_ROOT / "src" / "core" / "engine" / "remote" / name).read_text()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & {"socket", "urllib", "http", "requests", "boto3",
                               "ssl", "asyncio"}, (name, sorted(imported))


# --------------------------------------------------------------------------- #
# 8. the module allowlist
# --------------------------------------------------------------------------- #
def test_the_child_loads_only_the_allowlisted_harness_modules(tmp_path):
    """Twice an `__init__` re-export has quietly widened the boundary, and both
    times a graph walk caught it because someone ran one. This fails by name on
    the next import instead."""
    import os

    from src.core.engine.remote.adapter import (
        CHILD_MODULE_ALLOWLIST, CHILD_MODULE_FORBIDDEN,
    )
    from src.core.engine.remote.protocol import recv

    walk = REPO_ROOT / "docs" / "reviews" / "task26" / "_walk_in_child.py"
    to_child_r, to_child_w = os.pipe()
    to_parent_r, to_parent_w = os.pipe()
    os.set_inheritable(to_child_r, True)
    os.set_inheritable(to_parent_w, True)
    proc = subprocess.Popen(
        [sys.executable, str(walk), str(REPO_ROOT),
         str(REPO_ROOT / "src" / "strategies" / "current.py")],
        close_fds=False, cwd=str(REPO_ROOT),
        env=dict(os.environ, REMOTE_READ_FD=str(to_child_r),
                 REMOTE_WRITE_FD=str(to_parent_w)),
    )
    os.close(to_child_r)
    os.close(to_parent_w)
    reply = recv(to_parent_r)
    os.close(to_child_w)
    os.close(to_parent_r)
    proc.wait(timeout=30)

    loaded = set(reply["src_modules"])
    # the strategy's own module is named after where it lived
    strategy_modules = {m for m in loaded if m.endswith("._remote_strategy")}
    unexpected = loaded - CHILD_MODULE_ALLOWLIST - strategy_modules
    assert not unexpected, (
        "the strategy process loaded harness modules outside the allowlist:\n  "
        + "\n  ".join(sorted(unexpected))
        + "\n\nEither the import is needed -- add it to CHILD_MODULE_ALLOWLIST "
          "with a reason -- or an __init__ re-export widened the boundary again."
    )
    for forbidden in CHILD_MODULE_FORBIDDEN:
        assert not any(m == forbidden or m.startswith(forbidden + ".")
                       for m in loaded), forbidden
    assert reply["walked"] > 1000
    assert not reply["hits"], reply["hits"]


# --------------------------------------------------------------------------- #
# the protocol allowlist is a second copy of a signature
# --------------------------------------------------------------------------- #
def test_the_order_allowlist_matches_the_signature_it_guards():
    """**Measured drift, 2026-10-14, costing 16 trials.** `ACTIONS["buy"]` is a
    hand-maintained copy of `BaseContext._order`'s keyword-only parameters, and
    the two had drifted in both directions at once:

      * `target` was accepted by `_order` and documented in
        `docs/agent/strategy-api.md` -- the file pasted into every proposal
        prompt, which says absolute `stop`/`target` prices are "usually what you
        mean" -- but was absent here, so `sell does not take ['target']` fired
        13 times across the session campaigns. `stop` was allowed and `target`
        was not.
      * `expire_ns` was allowed here and does not exist anywhere else: `_order`
        takes `expire_ts`, and so does the documentation in four places
        including the error text `a GTD order needs expire_ts=<nanoseconds>`.
        Every GTD order the documentation asked for was refused by the
        transport, 3 times, and the key that was permitted could never be sent.

    In both directions the agent did what it was told and the protocol refused
    it, which is the most expensive kind of bug this repo can have: it is
    charged to the research budget as a crashed trial, and a crashed trial still
    counts toward N.
    """
    import inspect

    from src.core.engine.context import BaseContext
    from src.core.engine.remote.protocol import ACTIONS

    signature = {name for name, param
                 in inspect.signature(BaseContext._order).parameters.items()
                 if param.kind is param.KEYWORD_ONLY}
    for op in ("buy", "sell"):
        allowed = ACTIONS[op] - {"size"}          # size is positional on _order
        assert allowed == signature, (
            f"{op} allowlist has drifted from _order's signature.\n"
            f"  accepted by _order, refused by the protocol: "
            f"{sorted(signature - allowed)}\n"
            f"  allowed by the protocol, unknown to _order: "
            f"{sorted(allowed - signature)}")


def test_the_documented_bracket_keywords_all_survive_the_protocol():
    """The agent is shown `stop`/`target` as absolute prices and
    `stop_offset`/`target_offset` as positive distances. All four have to reach
    the engine, or the document is lying to the proposer."""
    from src.core.engine.remote.protocol import ACTIONS, validate

    for key in ("stop", "target", "stop_offset", "target_offset", "expire_ts"):
        assert key in ACTIONS["buy"], f"{key} is documented but not transportable"
        validate([{"op": "buy", "size": 1, key: 1.0}])


def test_an_unknown_keyword_is_still_refused():
    """Widening the allowlist must not turn it into a sieve."""
    import pytest as _pytest

    from src.core.engine.remote.protocol import ProtocolError, validate

    with _pytest.raises(ProtocolError):
        validate([{"op": "buy", "size": 1, "unlock_holdout": True}])
    with _pytest.raises(ProtocolError):
        validate([{"op": "buy", "size": 1, "expire_ns": 1}])
