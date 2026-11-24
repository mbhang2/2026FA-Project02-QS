#!/usr/bin/env python
"""Run one Numba kernel on the native engine, under one ledger row (Amendment 24).

Until Amendment 24 is signed this accepts only the committed check kernel and only a ledger under
`.scratch/`. The kernel runs in a child process. Its fills are validated, then audited by running
the kernel source through the Python engine behind `RemoteStrategy`, and only then scored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

# Scoring is imported before `run`, so the fingerprint `run` takes at import covers every
# scoring module this script uses and the integrity check compares like with like.
from src.core.scoring.metrics import session_calendar  # noqa: E402
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
from src.core.runlog import LedgerRun, RunTicket, append_ledger  # noqa: E402
from src.core.types import Side, Split  # noqa: E402

SHORT_AUDIT_EVENTS = 250_000
DEEP_AUDIT_SESSIONS = 5
SCRATCH = REPO_ROOT / ".scratch"
OPEN_DIR = "open-native-trials"


def _journal_dir(ledger_path: Path) -> Path:
    return Path(ledger_path).parent / OPEN_DIR


def recover_open_trials(ledger_path: Path, row_builder) -> int:
    """Turn every open-trial entry with no finished row into a crashed row. Returns how many.

    An entry is written before a kernel is parsed and removed only after its row is appended,
    so an entry left behind is a trial whose process was killed. It still counts toward N.
    """
    folder = _journal_dir(ledger_path)
    if not folder.is_dir():
        return 0
    finished = set()
    if Path(ledger_path).is_file():
        for line in Path(ledger_path).read_text().splitlines():
            if line.strip():
                finished.add(json.loads(line).get("trial_id"))
    recovered = 0
    for entry in sorted(folder.glob("*.json")):
        opened = json.loads(entry.read_text())
        if opened.get("trial_id") not in finished:
            append_ledger(Path(ledger_path), row_builder({
                "engine": "native", "outcome": "crashed", "failure_kind": "killed_without_row",
                "recovered": True, "score": None, "split": str(Split.TRAIN), **opened}))
            recovered += 1
        entry.unlink()
    return recovered


class _AuditSessions:
    """The audited sessions, the first optionally cut to its leading trades."""

    def __init__(self, inner, sessions, first_trades=None):
        self._inner, self._kept, self._first_trades, self.name = inner, list(sessions), first_trades, inner.name

    def __getattr__(self, key):
        return getattr(self._inner, key)

    def session_columns(self, split, *, warmup=0):
        for columns in self._inner.session_columns(split, warmup=0):
            if columns.session not in self._kept:
                continue
            if self._first_trades is not None and columns.session == self._kept[0]:
                n = self._first_trades
                columns = SessionColumns(columns.session, columns.ts[:n], columns.price[:n], columns.size[:n],
                                         columns.side[:n])
            yield columns

    def ticks(self, split, *, warmup=0):
        """The same trades as `session_columns`, so the engine reads those columns rather than
        falling back to the full split through the wrapped dataset's own `ticks`."""
        for columns in self.session_columns(split, warmup=warmup):
            for i in range(len(columns)):
                yield columns[i]


def _child_run(spec: dict) -> dict:
    opened = Path(spec["journal"])
    entry = json.loads(opened.read_text()) if opened.is_file() else {}
    if not opened.resolve().parent.name == OPEN_DIR or entry.get("trial_id") != spec["trial_id"] \
            or entry.get("kernel_sha256") != hashlib.sha256(spec["source"].encode()).hexdigest():
        raise T.NativeTrialRefused("the native child runs only for an open trial with this kernel")
    ticket = RunTicket.for_native_child(spec["trial_id"])
    cfg = run.load_config()
    profile = cfg["profiles"][spec["profile"]]
    if spec.get("latency_ns") is not None:
        profile = {**profile, "latency_ns": spec["latency_ns"]}
    dataset = run.build_dataset(cfg, profile["dataset"], profile["seed"])
    terms = run.execution_terms(profile)
    warmup = int(profile.get("warmup_sessions", 0))
    warm = set(dataset.warmup_sessions(Split.TRAIN, warmup))
    keep = set(spec["sessions"])
    kernel = K.CompiledKernel(spec["source"])
    lib = native.load()
    state_at_start, first_seq = None, None
    with native.NativeEngine(lib, ticket=ticket, latency_ns=terms["latency_ns"], require_two_sided=run._two_sided_book(profile),
                             book_features=True, book_events=True) as fast:
        for columns in dataset.session_columns(Split.TRAIN, warmup=warmup):
            is_warm = columns.session in warm
            if not is_warm and columns.session not in keep:
                continue
            if not is_warm and state_at_start is None:
                state_at_start, first_seq = kernel.state.copy(), fast.seq + 1
            fast.run_session(dataset.book_for(columns.session),
                             (columns.ts, columns.price, columns.size, columns.side), columns.session,
                             dataset.session_bounds(columns.session)[1], actionable=not is_warm,
                             callback=kernel.callback(lib), user=kernel.user_pointer())
            if lib.fc_engine_fill_count(fast.handle) > MAX_FILL_LOG:
                raise T.NativeTrialFailed(f"the kernel exceeded {MAX_FILL_LOG:,} fills")
        if state_at_start is None:
            raise T.NativeTrialFailed("no scored session ran")
        return {**fast.fill_columns(), "_state": state_at_start, "_first_seq": np.array([first_seq], np.int64)}


def _spawn_native(spec: dict, folder: str, timeout_s) -> tuple[dict, np.ndarray, int]:
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
    state, first = loaded.pop("_state"), loaded.pop("_first_seq")
    if state.dtype != np.float64 or state.ndim != 1 or first.shape != (1,) or first.dtype != np.int64 or first[0] < 0:
        raise T.NativeTrialFailed("the child returned an invalid audit start")
    return loaded, state, int(first[0])


def _audit(profile_name, profile, dataset, source, state, columns, first_seq, chosen, deep, ticket):
    """Replay the leading events in Python. Returns (engine, fills compared)."""
    if deep:
        audited, first_trades = chosen[:DEEP_AUDIT_SESSIONS], None
    else:
        audited = chosen[:1]
        trades = next(c for c in dataset.session_columns(Split.TRAIN, warmup=0) if c.session == audited[0])
        first_trades = T.trades_for_events(np.asarray(trades.ts), np.asarray(dataset.book_for(audited[0]).ts),
                                           SHORT_AUDIT_EVENTS)
    folder = SCRATCH / "native-audit"
    folder.mkdir(parents=True, exist_ok=True)
    text = T.audit_strategy_source(source, state)
    path = folder / f"{hashlib.sha256(text.encode()).hexdigest()[:16]}-{os.getpid()}.py"
    path.write_text(text)
    commission = ({"commission_per_contract": profile["commission_per_contract"]}
                  if "commission_per_contract" in profile else {})
    try:
        with RemoteStrategy(path, class_name="Strategy", action_cap=profile.get("action_cap", 64), window=1,
                            tick_window=1, timeout_s=profile.get("wall_clock_budget_s")) as strategy:
            engine = Engine(_AuditSessions(dataset, audited, first_trades), strategy, ticket=ticket, split=Split.TRAIN,
                            fill_model=profile["fill_model"], warmup_sessions=0, seed=profile["seed"],
                            commission_bps=profile.get("commission_bps", 0.0), book_features=True, book_events=True,
                            two_sided_book=run._two_sided_book(profile), **commission, **run.execution_terms(profile))
            engine.run()
            if engine.record.outcome != "completed":
                raise T.NativeTrialFailed(f"the audit did not complete: {engine.record.error}")
    finally:
        path.unlink(missing_ok=True)
    last = first_seq + engine._close_seq - 1
    keep = (columns["seq"] >= first_seq) & (columns["seq"] <= last)
    native_part = T.records_from_columns({k: v[keep] for k, v in columns.items()})
    log = engine.fill_log
    shifted = {"seq": np.array([f.seq + first_seq for f in log], np.int64), "ts": np.array([f.ts for f in log], np.int64),
               "session": np.array([f.session for f in log], np.int64),
               "order_id": np.array([f.order_id for f in log], np.int64),
               "side": np.array([1 if f.side is Side.BUY else -1 for f in log], np.int8),
               "size": np.array([f.size for f in log], np.int64),
               "price": np.array([f.price for f in log], np.float64),
               "maker": np.array([1 if f.liquidity == "maker" else 0 for f in log], np.int8),
               "mid": np.array([f.mid for f in log], np.float64)}
    if log_digest(native_part) != log_digest(T.records_from_columns(shifted)):
        raise T.AuditMismatch("the native fills differ from the Python audit")
    return engine, len(native_part), int(engine._close_seq), len(audited)


def run_trial(profile_name: str, kernel_path: Path, ledger_path: Path, *, sessions=None, deep=False,
              timeout_s=None, latency_ns: int | None = None) -> dict:
    """`latency_ns` overrides the profile for an unsigned check only, like `run.main`'s override."""
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
    purpose = "trial" if signed else "check"
    started = time.perf_counter()
    def build(r):
        return run._row(r, "human", profile_name, ledger_path, purpose, strategy_path=Path(kernel_path))

    recover_open_trials(ledger_path, build)
    trial_id = uuid.uuid4().hex
    journal = _journal_dir(ledger_path) / f"{trial_id}.json"
    run_log = LedgerRun(ledger_path, row_builder=build)
    try:
        with run_log:
            record = {"engine": "native", "outcome": "crashed", "split": str(Split.TRAIN), "score": None,
                      "trial_id": trial_id, "kernel_sha256": hashlib.sha256(source.encode()).hexdigest()}
            run_log.update(record)
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(json.dumps({"trial_id": trial_id, "kernel_sha256": record["kernel_sha256"],
                                           "profile": profile_name, "opened_unix": time.time()}))
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
                scored = list(dataset.scored_sessions(Split.TRAIN, 0))
                chosen = sorted(sessions) if sessions is not None else scored
                if not chosen or not set(chosen) <= set(scored):
                    raise T.NativeTrialRefused("sessions must be a non-empty part of the train split")
                spec = {"profile": profile_name, "source": source, "sessions": chosen, "latency_ns": latency_ns,
                        "trial_id": trial_id, "journal": str(journal)}
                with tempfile.TemporaryDirectory(prefix="native-trial-") as folder:
                    columns, state, first_seq = _spawn_native(
                        spec, folder, timeout_s if timeout_s is not None else profile.get("wall_clock_budget_s"))
                native_s = time.perf_counter() - started
                T.validate_columns(columns, set(chosen), dataset.session_bounds)
                audit_started = time.perf_counter()
                engine, audited_fills, audited_events, audited_sessions = _audit(
                    profile_name, profile, dataset, source, state, columns, first_seq, chosen, deep, run_log.ticket)
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
                result = score(blotter, period=period, min_exposed_fraction=cfg.get("campaign", {}).get("min_exposed_fraction"),
                               **kw)
                record.update(outcome="completed", dataset=dataset.name, dataset_digest=dataset.digest(),
                              fill_model=profile["fill_model"], seed=profile["seed"], fills=len(fills),
                              fill_log_digest=log_digest(fills), score=result.as_dict(),
                              diagnostics=score_diagnostics(blotter, period, **kw).as_dict(),
                              native_library_sha256=hashlib.sha256(native.LIBRARY.read_bytes()).hexdigest(),
                              native_abi=native.ABI_VERSION, numba_version=__import__("numba").__version__,
                              audit={"tier": "deep" if deep else "short", "sessions": audited_sessions,
                                     "events": audited_events, "fills": audited_fills, "matched": True},
                              native_seconds=round(native_s, 3), audit_seconds=round(audit_s, 3))
            except T.AuditMismatch as exc:
                record.update(outcome="invalidated_fill_log_mismatch", error=str(exc), score=None)
            except (K.KernelRejected, T.NativeTrialRefused) as exc:
                record.update(outcome="refused", failure_kind="native_refused", error=str(exc), score=None)
            except Exception as exc:  # noqa: BLE001 -- a failed trial is a row with an outcome
                record.update(outcome="crashed", failure_kind=type(exc).__name__, error=str(exc)[:1000], score=None)
            run_log.update(record)
    finally:
        if run_log.written:
            journal.unlink(missing_ok=True)
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
    record = run_trial(args.profile, args.kernel, args.ledger, deep=args.deep)
    print(json.dumps({k: record.get(k) for k in ("outcome", "fills", "audit", "native_seconds", "audit_seconds", "error")}))
    return 0 if record["outcome"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
