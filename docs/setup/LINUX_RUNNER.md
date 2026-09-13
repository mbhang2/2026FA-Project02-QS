# The Linux runner

Scored campaigns run on Linux. This is how to stand one up and how to check it is
actually isolating, rather than configured to.

`docs/design/architecture.md` explains why: macOS cannot set `RLIMIT_AS`,
`RLIMIT_DATA` or `RLIMIT_RSS` at all, and `sandbox-exec` has been documented as
deprecated for a decade. Both work well enough for development and neither is
something to run a campaign against.

---

## 1. A Linux VM on an Apple Silicon Mac

[Lima](https://lima-vm.io) gives a full arm64 Linux VM, unprivileged, with the
repo mounted. Colima and Docker Desktop also work; Lima is the smallest thing
that does the job.

```sh
brew install lima

limactl start --name=harness \
  --cpus=4 --memory=6 --disk=30 --vm-type=vz \
  --mount-writable=false --mount="$PWD:w" \
  template:ubuntu-24.04 --tty=false

limactl shell harness            # you land in the same path as on the host
```

`--mount-writable=false` is deliberate: the VM reads the repo and never writes to
it, so nothing that happens inside can reach the ledger, the null tables or the
committed calendar. It also means **the runner cannot write ledger rows into the
host repo** — run `run.py --ledger` at a VM-local path and copy the rows out, or
mount writable and accept that the VM can write to the repo. Choose deliberately;
the default here is the safe one.

## 2. Inside the VM

```sh
sudo apt-get update
sudo apt-get install -y bubblewrap python3-venv python3-pip

python3 -m venv ~/hv
~/hv/bin/pip install -r requirements.lock      # or requirements-dev.txt
```

### The one privileged step, and why it exists

Ubuntu 23.10 and later restrict unprivileged user namespaces through AppArmor
(`kernel.apparmor_restrict_unprivileged_userns = 1`), and Ubuntu 24.04 ships
`bubblewrap` **without** an AppArmor profile. Without one, `bwrap` fails with

```
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted     # with --unshare-net
bwrap: setting up uid map: Permission denied                     # without it
```

Neither message says "AppArmor". Install a profile once, as root:

```sh
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
sudo apparmor_parser -r /etc/apparmor.d/bwrap
```

**So the run is unprivileged and the setup is not.** The Task 24 spike said
"unprivileged user namespace, no privileges needed" and that was true of the
mechanism and not of getting to it on this distribution. A campaign machine needs
one root step at build time; after that nothing the harness does needs
privileges. `sysctl kernel.apparmor_restrict_unprivileged_userns=0` is the other
way and is worse — it removes the restriction for every process on the box, not
just `bwrap`.

## 3. Verify, then believe

```sh
~/hv/bin/python docs/reviews/task26/verify_isolation.py
```

Every check is a real run through `Engine` and `RemoteStrategy`, not an
inspection of the configuration meant to produce it. A configuration test passes
on a machine where the mechanism silently does nothing — which is exactly what
happened the first time this was run here, when a `--tmpfs /tmp` in the sandbox
made every probe "pass" with `FileNotFoundError`.

Expected on a correctly set up Linux box:

```
platform   Linux aarch64  python 3.12.3
isolation  isolated
           filesystem: read-only bind mount, unprivileged user namespace (bubblewrap)
           network:    empty network namespace (bubblewrap --unshare-net)
           memory:     RLIMIT_AS

  ok   goldens byte-identical through the boundary
  ok   escape walk inside the child                  21,198 objects, hits: none
  ok   module allowlist holds in the child
  ok   write to config/run.yaml                      OSError: [Errno 30] Read-only file system
  ok   write to runs/ledger.jsonl                    OSError: [Errno 30] Read-only file system
  ok   write to data/calibration/empirical-null.json OSError: [Errno 30] Read-only file system
  ok   write to src/strategies/current.py            OSError: [Errno 30] Read-only file system
  ok   network: DNS                                  gaierror: [Errno -3] Temporary failure in name resolution
  ok   network: raw TCP to a literal IP              OSError: [Errno 101] Network is unreachable
  ok   network: HTTP via urllib                      URLError: [Errno 101] Network is unreachable
  ok   RLIMIT_AS binds (allocate without bound)      MemoryError
  ok   RLIMIT_CPU is accepted and the run still completes
ALL CHECKS PASSED
```

If `isolation` says anything but `isolated`, the run record will say so too and
the rows produced are best-effort rows. That is a legitimate state to be in and
an illegitimate one to be in without noticing, which is why it is a field.

## 4. What it costs

Measured on the same real NQ validation split, 87 sessions, 33.2 M ticks, same
blotter digest `40fb3de977b37c2f` on both:

| | in process | isolated | overhead |
|---|---:|---:|---:|
| macOS 15.0, M1 Pro, 10 cores | 45.3 s | 49.3 s | **+8.8%** |
| Linux VM, 4 vCPU, bubblewrap | 61.0 s | 68.2 s | **+11.8%** |

The VM is slower in absolute terms — four virtual cores against ten, and
virtualised syscalls in the pipe path — and the *isolation* overhead is
comparable. Per callback the boundary costs 82 µs on macOS and 152 µs on Linux;
the difference is bubblewrap's namespace setup plus the virtualised pipe, and it
is invisible next to tick decoding on real data.

## 5. Do not mix platforms inside a campaign

The same trial on both machines gives an **identical blotter digest and an
identical fill-log digest**, and a Sortino that differs in the sixteenth digit:
−0.45305964538078264 on Linux against −0.45305964538078275 on macOS. `net_pnl` is
a float summation and numpy sums in a different order between builds; the digest
rounds and survives, the metric does not.

One ULP is not a correctness problem and it is a ranking problem: two rows scored
in different environments cannot be compared at that precision.
`scripts/ledger_check.py` flags the mismatch as **environment-drifted**, which is
what that flag is for. Pick one environment for a campaign and keep it.

## 6. Where campaigns should actually run

Not settled, and worth settling before the first one. A VM on the laptop verifies
the mechanism and can run a campaign, but a campaign is hours of wall clock on a
machine you also need. A small cloud Linux box or department compute would want
the R2 read-only key and `requirements.lock` to travel, and both are already
designed to.
