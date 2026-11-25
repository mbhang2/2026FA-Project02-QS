#!/usr/bin/env python
"""Run one Numba kernel on the native engine, under one ledger row (Amendment 24).

Until Amendment 24 is signed this accepts only the committed check kernel and only a ledger under
`.scratch/`. The kernel runs in a child process. Its fills are validated, then audited by running
the kernel source through the Python engine behind `RemoteStrategy`, and only then scored.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

# Scoring is imported before `run`, so the fingerprint `run` takes at import covers every
# scoring module this script uses and the integrity check compares like with like.
from src.core.scoring.metrics import session_calendar, session_returns  # noqa: E402
from src.core.scoring.scorer import score, score_diagnostics  # noqa: E402
from src.core.scoring.terms import scoring_terms  # noqa: E402
import prereg  # noqa: E402
import run  # noqa: E402
from src.core.data.columns import SessionColumns  # noqa: E402
from src.core.engine import Engine, kernel as K, native  # noqa: E402
from src.core.engine import native_trial as T  # noqa: E402
from src.core.engine.engine import MAX_FILL_LOG  # noqa: E402
from src.core.engine.reconstruct import log_digest, reconstruct  # noqa: E402
from src.core.engine.remote import RemoteStrategy  # noqa: E402
from src.core.runlog import LedgerRun, RunTicket, append_ledger, store_fill_log  # noqa: E402
from src.core.types import Side, Split  # noqa: E402

SHORT_AUDIT_EVENTS = 250_000
DEEP_AUDIT_SESSIONS = 5
SCRATCH = REPO_ROOT / ".scratch"
OPEN_DIR = "open-native-trials"


# --------------------------------------------------------------------------- #
# open-trial journal (A2)
# --------------------------------------------------------------------------- #
def _journal_dir(ledger_path: Path) -> Path:
    return Path(ledger_path).resolve().parent / OPEN_DIR


@contextmanager
def _ledger_lock(ledger_path: Path):
    """One process at a time opens or recovers entries for a ledger."""
    folder = _journal_dir(ledger_path)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield folder
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def _row_ids(ledger_path: Path) -> set:
    ids = set()
    if Path(ledger_path).is_file():
        for line in Path(ledger_path).read_text().splitlines():
            if line.strip():
                try:
                    ids.add(json.loads(line).get("trial_id"))
                except json.JSONDecodeError:
                    continue
    return ids


def open_trial(ledger_path: Path, trial_id: str, source: str, profile_name: str, purpose: str) -> tuple:
    """Write and lock this trial's entry. Returns (entry path, source copy, nonce, lock handle).

    The entry holds the kernel source as it was at the start, so the row stores that source even
    if the file is edited during the run. The lock tells recovery the trial is still alive.
    """
    nonce = secrets.token_hex(16)
    with _ledger_lock(ledger_path) as folder:
        copy = folder / f"{trial_id}.py"
        _write_atomic(copy, source)
        entry = folder / f"{trial_id}.json"
        _write_atomic(entry, json.dumps({
            "trial_id": trial_id, "kernel_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "profile": profile_name, "purpose": purpose, "nonce": nonce,
            "ledger": str(Path(ledger_path).resolve()), "opened_unix": time.time()}))
        handle = open(entry)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return entry, copy, nonce, handle


def recover_open_trials(ledger_path: Path, row_builder) -> int:
    """Turn every abandoned entry with no finished row into a crashed row. Returns how many.

    `row_builder(record, entry, source_path)` builds the row from the entry's own profile,
    purpose and source. An entry whose lock is held belongs to a live trial and is skipped. An
    entry that cannot be read still becomes a row, because it was a trial.
    """
    recovered = 0
    with _ledger_lock(ledger_path) as folder:
        finished = _row_ids(ledger_path)
        for entry in sorted(folder.glob("*.json")):
            try:
                handle = open(entry)
            except FileNotFoundError:
                continue
            with handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    opened = json.loads(handle.read())
                except json.JSONDecodeError:
                    opened = {"trial_id": entry.stem, "profile": "unknown", "purpose": "trial",
                              "unreadable_entry": True}
                copy = entry.with_suffix(".py")
                if opened.get("trial_id") not in finished:
                    record = {"engine": "native", "outcome": "crashed", "failure_kind": "killed_without_row",
                              "recovered": True, "score": None, "split": str(Split.TRAIN),
                              **{k: v for k, v in opened.items() if k != "nonce"}}
                    append_ledger(Path(ledger_path), row_builder(record, opened, copy if copy.exists() else None))
                    recovered += 1
                entry.unlink(missing_ok=True)
                copy.unlink(missing_ok=True)
    return recovered


def close_trial(ledger_path: Path, trial_id: str, entry: Path, copy: Path, handle) -> bool:
    """Remove the entry only once this trial's row is on disk. Returns whether it was."""
    try:
        if trial_id in _row_ids(ledger_path):
            entry.unlink(missing_ok=True)
            copy.unlink(missing_ok=True)
            return True
        return False
    finally:
        handle.close()


# --------------------------------------------------------------------------- #
# the native child (A3)
# --------------------------------------------------------------------------- #
def _check_child_spec(spec: dict) -> RunTicket:
    """Refuse unless this child belongs to a live, open trial of this kernel on this ledger."""
    ledger = Path(spec["ledger"]).resolve()
    entry_path = Path(spec["journal"]).resolve()
    if entry_path != (_journal_dir(ledger) / f"{spec['trial_id']}.json").resolve() or not entry_path.is_file():
        raise T.NativeTrialRefused("the native child runs only from its trial's entry beside the ledger")
    entry = json.loads(entry_path.read_text())
    sha = hashlib.sha256(spec["source"].encode()).hexdigest()
    if (entry.get("trial_id"), entry.get("kernel_sha256"), entry.get("ledger")) != (spec["trial_id"], sha, str(ledger)) \
            or not secrets.compare_digest(str(entry.get("nonce", "")), str(spec.get("nonce", ""))):
        raise T.NativeTrialRefused("the native child's spec does not match its trial's entry")
    with open(entry_path) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass                                   # held by the live parent, as it should be
        else:
            raise T.NativeTrialRefused("no live trial holds this entry")
    if not prereg.in_force(24):
        if spec["source"] != T.CHECK_KERNEL:
            raise T.NativeTrialRefused("Amendment 24 is not in force, so only the committed check kernel may run")
        if not ledger.is_relative_to(SCRATCH.resolve()):
            raise T.NativeTrialRefused("Amendment 24 is not in force, so the ledger must be under .scratch/")
    return RunTicket.for_native_child(spec["trial_id"])


def _child_run(spec: dict) -> dict:
    ticket = _check_child_spec(spec)
    cfg = run.load_config()
    profile = cfg["profiles"][spec["profile"]]
    if spec.get("latency_ns") is not None:
        profile = {**profile, "latency_ns": spec["latency_ns"]}
    dataset = run.build_dataset(cfg, profile["dataset"], profile["seed"])
    terms = run.execution_terms(profile)
    warmup = int(profile.get("warmup_sessions", 0))
    split = Split(spec["split"])
    warm = set(dataset.warmup_sessions(split, warmup))
    keep = set(spec["sessions"])
    points = {int(k): int(v) for k, v in spec["snapshots"].items()}
    kernel = K.CompiledKernel(spec["source"])
    lib = native.load()
    saved = {}
    with native.NativeEngine(lib, ticket=ticket, latency_ns=terms["latency_ns"],
                             require_two_sided=run._two_sided_book(profile), book_features=True,
                             book_events=True, inventory_cap=int(profile.get("inventory_cap_lots") or 0)) as fast:
        for columns in dataset.session_columns(split, warmup=warmup):
            is_warm = columns.session in warm
            if not is_warm and columns.session not in keep:
                continue
            fast.run_session(dataset.book_for(columns.session),
                             (columns.ts, columns.price, columns.size, columns.side), columns.session,
                             dataset.session_bounds(columns.session)[1], actionable=not is_warm,
                             callback=kernel.callback(lib), user=kernel.user_pointer(),
                             snapshot_trade=points.get(columns.session, -1), state=kernel.state)
            if columns.session in points:
                saved[columns.session] = fast.snapshot()
            if lib.fc_engine_fill_count(fast.handle) > MAX_FILL_LOG:
                raise T.NativeTrialFailed(f"the kernel exceeded {MAX_FILL_LOG:,} fills")
        out = fast.fill_columns()
    for i, session in enumerate(sorted(points)):
        snap = saved.get(session)
        if snap is None:
            raise T.NativeTrialFailed(f"no snapshot was taken in session {session}")
        out[f"_snap{i}_ints"] = np.array([session, snap["seq"], snap["position"], snap["next_id"], snap["trade"]],
                                         np.int64)
        out[f"_snap{i}_price"] = np.array([snap["last_price"]], np.float64)
        out[f"_snap{i}_orders_i"], out[f"_snap{i}_orders_d"] = snap["order_ints"], snap["order_doubles"]
        out[f"_snap{i}_state"] = snap["state"]
    return out


def _spawn_native(spec: dict, folder: str, timeout_s) -> tuple[dict, dict]:
    out = Path(folder) / "native.npz"
    # The spec goes over an inherited pipe, never a path on the command line.
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(write_fd, "w") as pipe:
            pipe.write(json.dumps({**spec, "out": str(out)}))
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child"], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=timeout_s, check=False, pass_fds=(read_fd,),
                              env={**os.environ, "PYTHONHASHSEED": "0", "NATIVE_TRIAL_SPEC_FD": str(read_fd)})
    finally:
        os.close(read_fd)
    if done.returncode != 0 or not out.exists():
        raise T.NativeTrialFailed(f"the native child failed: {done.stderr[-800:]}")
    with np.load(out, allow_pickle=False) as data:
        loaded = {name: data[name] for name in data.files}
    snapshots = {}
    for i, (session, trade) in enumerate(sorted((int(k), int(v)) for k, v in spec["snapshots"].items())):
        ints, price = loaded.pop(f"_snap{i}_ints"), loaded.pop(f"_snap{i}_price")
        orders_i, orders_d, state = (loaded.pop(f"_snap{i}_orders_i"), loaded.pop(f"_snap{i}_orders_d"),
                                     loaded.pop(f"_snap{i}_state"))
        good = (ints.dtype == np.int64 and ints.shape == (5,) and int(ints[0]) == session and int(ints[4]) == trade
                and int(ints[1]) >= -1 and int(ints[3]) >= 1 and price.dtype == np.float64 and price.shape == (1,)
                and orders_i.dtype == np.int64 and orders_i.ndim == 2 and orders_i.shape[1] == 14
                and orders_d.dtype == np.float64 and orders_d.shape == (orders_i.shape[0], 2)
                and state.dtype == np.float64 and state.ndim == 1)
        if not good:
            raise T.NativeTrialFailed("the child returned an invalid snapshot")
        snapshots[session] = {"seq": int(ints[1]), "position": int(ints[2]), "next_id": int(ints[3]),
                              "trade": trade, "last_price": float(price[0]), "order_ints": orders_i,
                              "order_doubles": orders_d, "state": state}
    if any(name.startswith("_") for name in loaded):
        raise T.NativeTrialFailed("the child returned unexpected arrays")
    return loaded, snapshots


# --------------------------------------------------------------------------- #
# the audit (B1 to B4)
# --------------------------------------------------------------------------- #
class _AuditWindow:
    """Consecutive segments (session, first trade, trade count or None for the rest)."""

    def __init__(self, inner, segments):
        self._inner, self._segments, self.name = inner, list(segments), inner.name

    def __getattr__(self, key):
        return getattr(self._inner, key)

    def session_columns(self, split, *, warmup=0):
        wanted = {session: (start, count) for session, start, count in self._segments}
        for columns in self._inner.session_columns(split, warmup=0):
            if columns.session not in wanted:
                continue
            start, count = wanted.pop(columns.session)
            cut = slice(start, None if count is None else start + count)
            yield SessionColumns(columns.session, columns.ts[cut], columns.price[cut], columns.size[cut],
                                 columns.side[cut])
            if not wanted:
                return

    def ticks(self, split, *, warmup=0):
        """The same trades as `session_columns`, so the engine reads those columns rather than
        falling back to the full split through the wrapped dataset's own `ticks`."""
        for columns in self.session_columns(split, warmup=warmup):
            for i in range(len(columns)):
                yield columns[i]


def _session_columns(dataset, session, split=Split.TRAIN):
    return next(c for c in dataset.session_columns(split, warmup=0) if c.session == session)


def plan_windows(dataset, chosen: list[int], deep: bool, rng=None, split=Split.TRAIN) -> list[list[tuple]]:
    """Audit windows, each a list of consecutive (session, first trade, trade count or None).

    Drawn from the operating system's randomness here, after the trial opens. The kernel runs in
    the child and sees only its own arrays, so it cannot know where any window starts.

    Short tier: a start drawn uniformly over every trade of the run, then 250,000 events, crossing
    session closes. Deep tier: five sessions drawn uniformly, each from the last trade of the
    session before it, so the close, the overnight orders and the open are all compared.
    """
    rng = rng or random.SystemRandom()
    sizes = {}
    for columns in dataset.session_columns(split, warmup=0):
        if columns.session in set(chosen):
            sizes[columns.session] = len(columns)
    order = [s for s in chosen if s in sizes]
    if deep:
        windows, starts = [], set()
        for target in sorted(rng.sample(order, min(DEEP_AUDIT_SESSIONS, len(order)))):
            i = order.index(target)
            prev = order[i - 1] if i else None
            if prev is not None and prev not in starts:
                windows.append([(prev, sizes[prev] - 1, None), (target, 0, None)])
                starts.add(prev)
            elif target not in starts:
                windows.append([(target, 0, None)])
                starts.add(target)
        return windows
    total = sum(sizes[s] for s in order)
    pick = rng.randrange(total)
    index = 0
    while pick >= sizes[order[index]]:
        pick -= sizes[order[index]]
        index += 1
    segments, budget, start = [], SHORT_AUDIT_EVENTS, pick
    for session in order[index:]:
        ts = np.asarray(_session_columns(dataset, session, split).ts)
        after = int(ts[start - 1]) if start else int(ts[0])
        book_ts = np.asarray(dataset.book_for(session).ts)
        count = T.trades_for_events(ts[start:], book_ts, budget, after)
        budget -= T.events_in(ts[start:start + count], book_ts, after)
        segments.append((int(session), int(start), None if start + count >= ts.size else int(count)))
        start = 0
        if budget <= 0:
            break
    return [segments]


def _audit_window(profile, dataset, source, columns, snap, segments, ticket, split=Split.TRAIN) -> tuple:
    """Resume the Python engine at the saved point and require the same fills. Returns
    (engine, record of the window)."""
    session, start, _ = segments[0]
    ts = np.asarray(_session_columns(dataset, session, split).ts)
    T.check_snapshot(snap, columns, start)
    resume = T.resume_from_snapshot(snap, int(ts[start - 1]) if start else int(ts[0]))
    folder = SCRATCH / "native-audit"
    folder.mkdir(parents=True, exist_ok=True)
    text = T.audit_strategy_source(source, snap["state"], snap["last_price"])
    path = folder / f"{hashlib.sha256(text.encode()).hexdigest()[:16]}-{os.getpid()}.py"
    path.write_text(text)
    commission = ({"commission_per_contract": profile["commission_per_contract"]}
                  if "commission_per_contract" in profile else {})
    try:
        with RemoteStrategy(path, class_name="Strategy", action_cap=profile.get("action_cap", 64), window=1,
                            tick_window=1, timeout_s=profile.get("wall_clock_budget_s")) as strategy:
            engine = Engine(_AuditWindow(dataset, segments), strategy, ticket=ticket, split=split,
                            fill_model=profile["fill_model"], warmup_sessions=0, seed=profile["seed"],
                            commission_bps=profile.get("commission_bps", 0.0), book_features=True, book_events=True,
                            two_sided_book=run._two_sided_book(profile), resume=resume,
                            inventory_cap=profile.get("inventory_cap_lots"), **commission,
                            **run.execution_terms(profile))
            engine.run()
            if engine.record.outcome != "completed":
                raise T.NativeTrialFailed(f"the audit did not complete: {engine.record.error}")
    finally:
        path.unlink(missing_ok=True)
    first = resume["first_seq"]
    last = first + engine._close_seq - 1
    keep = (columns["seq"] >= first) & (columns["seq"] <= last)
    native_part = T.records_from_columns({k: v[keep] for k, v in columns.items()})
    log = engine.fill_log
    shifted = {"seq": np.array([f.seq + first for f in log], np.int64), "ts": np.array([f.ts for f in log], np.int64),
               "session": np.array([f.session for f in log], np.int64),
               "order_id": np.array([f.order_id for f in log], np.int64),
               "side": np.array([1 if f.side is Side.BUY else -1 for f in log], np.int8),
               "size": np.array([f.size for f in log], np.int64),
               "price": np.array([f.price for f in log], np.float64),
               "maker": np.array([1 if f.liquidity == "maker" else 0 for f in log], np.int8),
               "mid": np.array([f.mid for f in log], np.float64)}
    if log_digest(native_part) != log_digest(T.records_from_columns(shifted)):
        raise T.AuditMismatch(f"the native fills differ from the Python audit from session {session} trade {start}")
    return engine, {"segments": [list(s) for s in segments], "events": int(engine._close_seq),
                    "fills": len(native_part)}


# --------------------------------------------------------------------------- #
# one trial
# --------------------------------------------------------------------------- #
def run_trial(profile_name: str, kernel_path: Path, ledger_path: Path, *, sessions=None, deep=False,
              timeout_s=None, latency_ns: int | None = None, audit_of: str | None = None,
              validates: str | None = None, train_profile: str | None = None) -> dict:
    """`latency_ns` overrides the profile for an unsigned check only, like `run.main`'s override.

    `deep` is Amendment 24 B3's audit of a trial that passed train. It names that trial in
    `audit_of`, and its row has purpose `audit`, so it is not another trial in N.
    """
    cfg = run.load_config()
    profile = cfg["profiles"].get(profile_name)
    unknown = profile is None
    profile = profile or {}
    source = Path(kernel_path).read_text()
    signed = prereg.in_force(24)
    if not signed:
        if source != T.CHECK_KERNEL:
            raise T.NativeTrialRefused("Amendment 24 is not in force, so only the committed check kernel may run")
        if not Path(ledger_path).resolve().is_relative_to(SCRATCH.resolve()):
            raise T.NativeTrialRefused("Amendment 24 is not in force, so the ledger must be under .scratch/")
    elif profile.get("engine") != "native":
        raise T.NativeTrialRefused("the profile does not set engine: native")
    if latency_ns is not None:
        if signed:
            raise T.NativeTrialRefused("a latency override is for an unsigned check only")
        profile = {**profile, "latency_ns": latency_ns}
    if deep and signed and not audit_of:
        raise T.NativeTrialRefused("a deep audit names the trial it verifies in audit_of")
    if audit_of and not deep:
        raise T.NativeTrialRefused("only a deep audit names another trial")
    split = Split(profile["split"]) if "split" in profile else Split.TRAIN
    if split is Split.VALIDATION:
        # Amendment 24 B3: validation opens only for a trial whose deep audit matched.
        if deep or not validates or not train_profile:
            raise T.NativeTrialRefused("a validation run names the train trial it validates and its profile")
        if not T.deep_audit_passed(ledger_path, validates, hashlib.sha256(source.encode()).hexdigest(),
                                   train_profile):
            raise T.NativeTrialRefused(f"trial {validates} has no matching deep audit, so validation stays closed")
    elif validates:
        raise T.NativeTrialRefused("only a validation run names the trial it validates")
    purpose = ("audit" if deep else "trial") if signed else "check"
    started = time.perf_counter()

    def recovered_row(record, entry, copy):
        return run._row(record, "human", entry.get("profile", "unknown"), ledger_path, entry.get("purpose", "trial"),
                        strategy_path=copy if copy is not None else Path(os.devnull))

    recover_open_trials(ledger_path, recovered_row)
    trial_id = uuid.uuid4().hex
    entry, copy, nonce, handle = open_trial(ledger_path, trial_id, source, profile_name, purpose)
    run_log = LedgerRun(ledger_path, row_builder=lambda r: run._row(r, "human", profile_name, ledger_path, purpose,
                                                                    strategy_path=copy))
    try:
        with run_log:
            record = {"engine": "native", "outcome": "crashed", "split": str(split), "score": None,
                      "trial_id": trial_id, "kernel_sha256": hashlib.sha256(source.encode()).hexdigest(),
                      **({"audit_of": audit_of} if audit_of else {}),
                      **({"validates": validates} if validates else {})}
            run_log.update(record)
            try:
                if unknown:
                    raise T.NativeTrialRefused(f"unknown profile {profile_name!r}")
                K.check(source)
                problem = T.refusal(profile)
                if problem:
                    raise T.NativeTrialRefused(problem)
                if not prereg.in_force(22):
                    raise T.NativeTrialRefused("Amendment 22 is not in force")
                dataset = run.build_dataset(cfg, profile["dataset"], profile["seed"])
                scored = list(dataset.scored_sessions(split, 0))
                chosen = sorted(sessions) if sessions is not None else scored
                if not chosen or not set(chosen) <= set(scored):
                    raise T.NativeTrialRefused(f"sessions must be a non-empty part of the {split} split")
                windows = plan_windows(dataset, chosen, deep, split=split)
                record["audit"] = {"tier": "deep" if deep else "short", "planned": [[list(s) for s in w] for w in windows]}
                run_log.update(record)
                spec = {"profile": profile_name, "source": source, "sessions": chosen, "latency_ns": latency_ns,
                        "trial_id": trial_id, "journal": str(entry), "nonce": nonce, "split": str(split),
                        "ledger": str(Path(ledger_path).resolve()),
                        "snapshots": {str(w[0][0]): w[0][1] for w in windows}}
                with tempfile.TemporaryDirectory(prefix="native-trial-") as folder:
                    columns, snapshots = _spawn_native(
                        spec, folder, timeout_s if timeout_s is not None else profile.get("wall_clock_budget_s"))
                native_s = time.perf_counter() - started
                T.validate_columns(columns, set(chosen), dataset.session_bounds)
                audit_started = time.perf_counter()
                audited = []
                for window in windows:
                    engine, info = _audit_window(profile, dataset, source, columns, snapshots[window[0][0]], window,
                                                 run_log.ticket, split)
                    audited.append(info)
                audit_s = time.perf_counter() - audit_started
                run.integrity.check(run.SCORING_AT_IMPORT, run.integrity.fingerprint())
                run.integrity.check_artifacts(run.ARTIFACTS_AT_IMPORT, run.integrity.artifact_digests())
                execution = run.execution_terms(profile)
                fills = T.records_from_columns(columns)
                blotter = reconstruct(fills, commission_per_contract=engine.commission,
                                      commission_bps=engine.commission_bps,
                                      slippage_per_contract=execution["slippage_per_contract"],
                                      slippage_bps=execution["slippage_bps"],
                                      maker_commission_per_contract=execution["maker_commission_per_contract"],
                                      maker_commission_bps=execution["maker_commission_bps"])
                period = session_calendar(dataset, chosen)
                terms = scoring_terms(cfg, profile, dataset, period)
                kw = dict(notional_base=terms["notional_base"], idle_charge=terms["idle_charge"], funding=terms["funding"])
                result = score(blotter, period=period,
                               min_exposed_fraction=cfg.get("campaign", {}).get("min_exposed_fraction"), **kw)
                record["audit"].update(windows=audited, matched=True)
                record.update(outcome="completed", dataset=dataset.name, dataset_digest=dataset.digest(),
                              fill_model=profile["fill_model"], seed=profile["seed"], fills=len(fills),
                              fill_log_digest=log_digest(fills), score=result.as_dict(),
                              fill_log_source=store_fill_log(Path(ledger_path), log_digest(fills), fills),
                              execution=execution,
                              session_returns=[float(x) for x in session_returns(blotter, period, **kw)],
                              diagnostics=score_diagnostics(blotter, period, **kw).as_dict(),
                              native_library_sha256=hashlib.sha256(native.LIBRARY.read_bytes()).hexdigest(),
                              native_abi=native.ABI_VERSION, numba_version=__import__("numba").__version__,
                              native_seconds=round(native_s, 3), audit_seconds=round(audit_s, 3))
            except T.AuditMismatch as exc:
                record.update(outcome="invalidated_fill_log_mismatch", error=str(exc), score=None)
            except (K.KernelRejected, T.NativeTrialRefused) as exc:
                record.update(outcome="refused", failure_kind="native_refused", error=str(exc), score=None)
            except Exception as exc:  # noqa: BLE001 -- a failed trial is a row with an outcome
                record.update(outcome="crashed", failure_kind=type(exc).__name__, error=str(exc)[:1000], score=None)
            run_log.update(record)
    finally:
        close_trial(ledger_path, trial_id, entry, copy, handle)
    return record


def _stop_on_terminate() -> None:
    """A terminated run still leaves its row. SIGTERM becomes SystemExit, which `LedgerRun` sees."""
    import signal

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--profile")
    ap.add_argument("--kernel", type=Path)
    ap.add_argument("--ledger", type=Path)
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--audit-of", dest="audit_of")
    args = ap.parse_args(argv)
    if args.child:
        fd = os.environ.get("NATIVE_TRIAL_SPEC_FD")
        if fd is None:
            ap.error("--child is started only by run_trial, which passes its spec over a pipe")
        with os.fdopen(int(fd)) as pipe:
            spec = json.loads(pipe.read())
        columns = _child_run(spec)
        temporary = spec["out"] + ".tmp.npz"
        np.savez(temporary, **columns)
        os.replace(temporary, spec["out"])
        return 0
    if not (args.profile and args.kernel and args.ledger):
        ap.error("--profile, --kernel and --ledger are required")
    _stop_on_terminate()
    record = run_trial(args.profile, args.kernel, args.ledger, deep=args.deep, audit_of=args.audit_of)
    print(json.dumps({k: record.get(k) for k in ("outcome", "fills", "audit", "native_seconds", "audit_seconds", "error")}))
    return 0 if record["outcome"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
