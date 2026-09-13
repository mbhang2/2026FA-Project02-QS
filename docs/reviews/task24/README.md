# Task 24 — process isolation: spike report

Investigation with a go/no-go, not a build. Everything here is throwaway and none
of it is imported by `src/`. Measured on the machine campaigns run on:
MacBookPro18,1 (M1 Pro, 10 cores), macOS 15.0 arm64, CPython 3.11.5.

**Recommendation: GO.** Push an incrementally-maintained window; serve no pulls.
Projected cost is **6.6% of a full 564-session NQ trial**, and the shape that
decides it is not the window size — it is that a rolling window is append-only
and does not need re-sending.

---

## 1. Callback volume

`measure_callbacks.py`, on the existing in-process engine. A proxy context and a
wrapper strategy count what crosses; the blotter is asserted identical to an
uninstrumented run, and instrumentation cost 0.4% of wall clock (44.81s → 44.97s).

Real NQ, pre-registered validation split, 87 scored sessions, 33,254,026 ticks:

| callback | measured | projected to 564 sessions |
|---|---:|---:|
| `on_bars` | 23,823 | 154,439 |
| `on_lifecycle` | 15,186 | 98,447 |
| `on_fill` | 7,422 | 48,115 |
| `setup` | 1 | 6 |
| **total** | **46,432** | **301,007** |

The event walk is why this is small: the engine stops the strategy at 46,432
points out of 33.2 million ticks — **0.14%**.

## 2. `ctx.history` volume — and the number that actually matters

| | measured | projected |
|---|---:|---:|
| `history()` calls | 23,823 | 154,439 |
| bars in the returned views | 283,779,576 | 1,839,674,493 |
| **bars the strategy read** | **23,823** | **154,439** |
| read / returned | **0.00008** | |

`ctx.history()` returns a lazy view over everything the run has seen — a mean of
11,912 bars by the end of the validation split — and the reference strategy reads
`history[-1]`. Serialising what the call *returns* is 219 GB per full trial.
Serialising what it *reads* is 18 MB. The view has to become a window somewhere,
and that is the whole protocol question.

## 3. Round-trip cost

`measure_roundtrip.py`: 2,000 length-prefixed frames per row over anonymous pipes
to a forked child that decodes and replies, medians.

| bars | codec | payload | median | p90 |
|---:|---|---:|---:|---:|
| 1 | json | 312 B | 14.2 µs | 19.5 µs |
| 1 | pickle | 253 B | 12.6 µs | 17.0 µs |
| 10 | json | 1,401 B | 19.8 µs | 25.5 µs |
| 200 | json | 24,391 B | 125.5 µs | 133.0 µs |
| 200 | pickle | 14,582 B | 51.2 µs | 59.3 µs |
| 20,000 | json | 2.4 MB | 17.5 ms | 18.8 ms |

`pickle` is 2.4× faster at 200 bars and **must not be used in the
strategy → engine direction**: unpickling a reply is arbitrary code execution by
the process the whole exercise exists to distrust. Engine → strategy may use it.
The prototype uses JSON both ways, so every number below is the pessimistic one.

## 4. Projected cost of a full trial

Baseline: 44.8 s over 87 sessions → **290 s** for 564. Per-callback costs are the
prototype's, measured as overhead over an in-process run of the same data — real
snapshot building, real JSON, real strategy work — not the microbenchmark floor.

| shape | µs/callback | full trial | of baseline |
|---|---:|---:|---:|
| push 1 bar | 55 | 16.7 s | **5.7%** |
| push 200 bars, resent every callback | 561 | 169.0 s | 58.2% |
| **push 200 bars, incremental** | **63** | **19.1 s** | **6.6%** |
| push 20,000 bars, incremental | 181 | 54.4 s | 18.7% |
| serve pulls, 1 pull per callback | 55 | 25.2 s | 8.7% |
| serve pulls, 10 pulls per callback | 55 | 183 s | 63% |

---

## The recommendation, and the number that decides it

**Push an incrementally-maintained window.** The engine declares a window size at
`setup()`; each callback carries only the bars the child has not seen — one,
almost always — and the child keeps its own deque of the declared size.

The deciding number is **561 µs → 63 µs**, a 9× drop, for the same 200-bar window
with the same semantics. Re-sending the window every callback costs 58% of a
trial; sending the delta costs 6.6%. A rolling window is append-only, so
re-sending it is sending the same bars again, and the fix is arithmetic rather
than protocol design.

**Serve pulls is rejected, and not on the reference strategy's numbers** — one
pull per callback is 8.7%, which is affordable. It is rejected because **nothing
bounds the number of pulls**. The action cap bounds orders per callback; there is
no read cap, and an agent optimising for its own score has no reason to
economise. Ten pulls per callback is 63% of a trial and the strategy chooses the
multiplier. A shape whose cost is a free parameter of the untrusted side is not a
shape, and adding a read cap to fix it is a new semantic the in-process engine
does not have.

**What the push shape costs in honesty**: a strategy cannot reach further back
than the declared window. The prototype's `Window` keeps `len()` exact — the true
total is sent alongside the tail — and raises `WindowTooShort` on an
out-of-window index. That is an error the in-process version cannot produce, and
it is the one real semantic difference. Making it loud rather than silent is the
right trade; a boundary that quietly truncates history is a boundary that changes
results.

---

## The prototype

`proto/protocol.py`, `proto/child.py`, `proto/adapter.py`, driven by
`proto_run.py`. Synchronous, deterministic, one request and one reply per
callback. The child gets two pipe fds and nothing else.

**Acceptance criterion met.** Every configuration produced a blotter *and* a fill
log byte-identical to the in-process run:

```
sessions scored   41   window 200 bars  (incremental)
in process        9a660a901b41a8ece1e0ae90fc818503c04b698811f93982272cc38bc627e995
through a pipe    9a660a901b41a8ece1e0ae90fc818503c04b698811f93982272cc38bc627e995
fill log          same   c623aaaf86d3eb6b
BYTE-IDENTICAL
```

Verified at 9, 41 and 81 scored sessions; windows of 1, 200 and 20,000; full
resend and incremental; sandboxed and not.

## Does the dataset need to cross? No.

`proto_imports_probe.py` reports the child's own `sys.modules` and walks its
object graph with `gc.get_referents` after a `setup()`:

```
objects walked in the child   5,387
engine/dataset reachable      none
open file descriptors         [0, 1, 2, 3, 4, 6]
src.* modules the child loaded:
   src.core
   src.core.scoring.calibration._remote_strategy
   src.core.types
```

`src.core.types` is frozen value types, enums and constants — `Side`,
`OrderType`, `TimeInForce`, `NS_PER_SECOND`, `Bar`. Data, with no dataset, engine
or file handle behind it.

**So second-pass finding 2 closes as a side effect.** `_SealedDataset.__getattr__`
forwards a bound method of the inner dataset, and `gc.get_referents` walks
straight through the seal to the raw `SyntheticDataset` — seed, calendar,
`unlock_holdout`, holdout tick. In an isolated build there is no dataset in the
strategy's address space to forward, so the escape is not patched, it is absent.
Finding 3 (`ctx._queue.cap` and `.actions` mutable) goes the same way: the queue
is on the engine side and the child sends a validated list of actions the engine
counts against its own cap.

The one thing that must be solved and is not solved here: `ctx.buy()` returns an
order id at the call, allocated by the intent queue. Across the boundary that
needs a pre-allocated block per callback — easy, deterministic, and the reference
strategy does not exercise it, so the prototype returns nothing and says so.

## Filesystem restriction, per platform

**macOS (this machine) — works, deprecated, and brittle.** `docs/reviews/task24/strategy.sb`
is enforcing on macOS 15.0 / arm64 with no privileges. `proto_sandbox_probe.py`
runs a strategy that rewrites a file from `setup()`:

```
unsandboxed  the run completed                          file on disk: True
sandboxed    StrategyCrashed: PermissionError [Errno 1] file on disk: False
```

Writes, sockets and further `exec` are all refused; reads and imports work; the
pipe still works; the cost is unmeasurable (0.36 s vs 0.36 s at 41 sessions).

The brittleness is real. `sandbox-exec(1)` has been documented as deprecated for
a decade. And because the profile is applied *before* the exec that starts the
child, the interpreter must be allowed by literal path — three of them, as it
turns out: the venv shim, its realpath, and the framework's `Python.app` stub.
Getting that wrong fails as `execvp() ... Operation not permitted` or
`posix_spawn: Undefined error: 0`, neither of which says what is wrong.

**Linux — not tested here; no container runtime on this machine** (no docker,
podman, colima or lima on `PATH`). The options, in the order I would try them:

1. **Unprivileged user namespace + a read-only bind mount** of the repo, with a
   tmpfs for anything the child may write. No root, available on any modern
   kernel with `unshare(CLONE_NEWUSER|CLONE_NEWNS)`. This is the one to build.
2. **seccomp**, via `libseccomp` or a small ctypes `prctl(PR_SET_SECCOMP)` filter
   installed by the child before it imports the strategy. Denies `openat` with
   write flags, `socket`, `execve`. No privileges. Finer-grained and easier to
   get subtly wrong than a read-only mount.
3. **A container.** The strongest and the heaviest; if campaigns run in CI or on
   a cloud box it is free, and if they run on a laptop it is a VM.

**Recommendation: scored runs happen on Linux.** The guarantee genuinely differs.
On Linux a read-only mount is a kernel-enforced property with no deprecated tool
in the path; on macOS it is a deprecated binary and a hand-maintained list of
interpreter paths. "Isolated on Linux, best-effort on macOS" is the honest
statement, and the ledger should record which — a row produced under best-effort
isolation is not the same evidence as one produced under a read-only mount.

### Resource bounds — measured, and macOS is worse than expected

| bound | macOS 15.0 / arm64 | note |
|---|---|---|
| `RLIMIT_CPU` | **enforced** | exit 152 = SIGXCPU, tested |
| `RLIMIT_NOFILE` | **enforced** | refused at 13 of 64 handles |
| `RLIMIT_AS` / `RLIMIT_DATA` / `RLIMIT_RSS` | **cannot even be set** | `ValueError: current limit exceeds maximum limit`, with either hard limit |
| wall clock | parent-side, always available | `Popen.wait(timeout=…)` then kill |

**There is no memory ceiling on macOS.** A strategy that balloons takes the
machine down with it. On Linux `RLIMIT_AS` works, and a cgroup memory limit works
better. This is a second, independent reason to run scored campaigns on Linux.

A subprocess that hangs or balloons becoming a trial with `outcome: crashed` is
cleaner than anything available in-process, and the prototype already does the
crash half: a strategy exception comes back as `StrategyCrashed` with the child's
traceback, and the engine is still standing to write the row.

---

## Size estimate for the real refactor

Not a number of days — what changes, and what I expect to be hard.

**Mechanical, low risk:**

- `run.py`, `scripts/nq_smoke.py`, `scripts/replay.py`: wrap the loaded strategy
  in the adapter. The `LedgerRun` block from Task 23 is already the right shape
  to hang process lifetime on — the subprocess starts and stops inside it.
- Test fixtures: `Engine.for_test` already funnels every one of them through one
  call site, so an in-process mode stays available for the ~80 tests that are
  about engine semantics rather than the boundary.
- `RunTicket`: a fourth kind, or a field saying which isolation the run had. The
  ledger row should say, and `ledger_check.py` should report it.

**The real work, in order of how much I expect it to hurt:**

1. **The context surface has to be enumerated exactly, and it is bigger than it
   looks.** The spike's `RemoteContext` implements 12 scalars plus `history`,
   `closed` and five actions, and that was enough only for the reference
   strategy. `BarContext` exposes 29 public names. Three of them —
   `find_orders`, `working_orders`, `modify` — return or take order objects and
   need a wire representation. `sub_id` and `now` read like scalars and are not
   (`sub_id` is a method; discovering that cost a `TypeError`). The right move is
   to **generate the adapter from `docs/design/context-api.md`**, which is
   already generated from the code, so the boundary cannot drift from the
   interface the way this prototype did three times in one afternoon.
2. **Order ids.** Pre-allocate a block per callback. Deterministic and small, but
   it touches `IntentQueue` allocation, which is where a Task 20 escape lived.
3. **`setup()` and the training split.** `setup()` streams the whole training
   split — 22 GiB on the real adapter — and it is the one place a bulk pull is
   unavoidable. It is also the last remaining reason `_SealedDataset` exists.
   Streaming it across the pipe in chunks is straightforward and is the single
   biggest payload in the design; it wants its own measurement before it is
   built.
4. **Error taxonomy.** `ContextError`, `LookaheadError` and the rest are raised
   on the engine side today and caught by nobody. Across the boundary they are
   raised in the child, and the child's traceback has to arrive intact or every
   agent-facing error message in `strategy-api.md` becomes a lie.
5. **Golden digests.** They must not move, and the prototype says they need not:
   every configuration reproduced them byte for byte. The risk is not the
   protocol, it is item 1 — a context attribute that is subtly different across
   the wire will change a decision somewhere and the golden set is what catches
   it. Run the full golden set through the boundary before believing anything.

**What I do not expect to be hard:** the engine's callback sites. The adapter
*is* a `Strategy`, so `engine.py` does not change at all. That is the strongest
argument for this design and the prototype demonstrates it — the engine ran a
subprocess-backed strategy without knowing.

---

## Go / no-go

**GO.** 6.6% of a trial, a byte-identical blotter, no dataset crossing the
boundary, and two open critical findings closing structurally rather than by
patch.

**The condition that flips it to no-go:** if `setup()`'s bulk training stream
turns out to cost more than the callbacks do. It is the one payload this spike did
not measure — the synthetic prototype's `setup()` sends an empty message, and on
the real adapter it is 22 GiB. Measure it before writing anything else. If
streaming the training split across the pipe costs more than about a quarter of a
trial, the design needs a shared-memory or file-descriptor-passing answer for that
one path, and that is a different and larger piece of work than the rest of this.

A second, softer condition: if the context enumeration in item 1 turns up
attributes that cannot be pushed *or* served without a round trip per access —
something like a lazily-computed view over orders that a strategy touches in a
loop — the per-callback cost stops being a constant and the projection above stops
holding.
