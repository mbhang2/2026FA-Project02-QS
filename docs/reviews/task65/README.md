# Task 65 — is the conditional null centred, where the cheap one is not?

**Pre-registered before anything ran.** Candidates, data, R, and what gets
reported are fixed at this commit.

## 1. The question, and why it is not "what is rotation's FWER"

Task 64 measured the cheap screen at **FWER 0.0020** against a nominal 0.05, and
located the cause: each candidate scores **0.2170 below the mean of its own
sign-flip null, 95% of the time**. The null is mis-centred, because edgeless
trading has negative expected gross under strict-through fills and flipping a
negative-mean variable centres the null above it.

The proposed remedy (Task 64 §"What this leaves", option 1) is to stop letting
the cheap null decide and route candidates to the **conditional** null instead.
That remedy rests on an assumption nobody has checked: **that rotation is
centred.** It should be — `rotation.py` re-runs the strategy against a rotated
price path, so the rotated arms incur adverse selection too — but "should be" is
what was said about the sign-flip null.

A full conditional FWER is unaffordable: C x N x R runs, and at N = 50, R = 150
even C = 100 is 750,000 runs. **The centring question does not need it.** It
needs one campaign's worth of candidates, each with its own rotation null, and
the same diagnostic Task 64 used.

## 2. The measurement

**100 candidates, R = 150 rotations each — 15,000 isolated runs, ~20 minutes at
8 workers.**

| knob | value |
|---|---|
| candidates | `NullGenerator(seed=64_001)`, indices 0-99 — **the same stream Task 64 used** |
| dataset | `SyntheticSpec(n_sessions=175, ticks_per_session=390, vol_per_tick=0.15, seed=4_242, signal_strength=0.0)` |
| rotations | R = 150, seeds `800_000 + i*R + r` (Task 38's block convention) |
| metric | `score(..., idle_charge=0.18)` — the current gate's metric |
| paired arm | `round_turn.null_sample(..., b=10_000, seed=17, idle_charge=0.18)` on the **same** candidates |

**It is paired on purpose.** The same candidates, the same data, the same metric,
two nulls. A difference between the arms is then a property of the nulls and not
of the sample.

## 3. What gets reported — fixed now

1. **`fraction_below`** — candidates whose observed score is below the mean of
   their own null. A centred null gives ~0.50. Task 64's sign-flip arm gave
   **0.95**.
2. **`mean_gap`** — mean of (observed - mean of its own null). Centred gives ~0.
   Sign-flip gave **-0.2170**.
3. Both, for **both arms**, from the same candidates.

Also recorded as context, not as a test: the dispersion ratio
(observed std / null std), and how many candidates produced no usable null.

**What each outcome would mean, stated before the numbers exist:**

- **Rotation near 0.50 and sign-flip near 0.95** — the remedy is sound. The cheap
  null is the broken one and routing to the conditional fixes the centring.
- **Both near 0.95** — the remedy is not sound, the problem is not specific to
  sign-flipping, and re-centring (Task 64 option 3) or a different statistic
  becomes the only route. This would be the expensive answer and it is the reason
  to spend twenty minutes before spending a week.
- **Rotation materially *above* 0.50** — rotation is anti-conservative, which
  would matter more than either of the above and would need its own task
  immediately.

## 4. What this does not establish

A centred null is necessary for a calibrated gate and not sufficient. This
measures location only. It says nothing about rotation's FWER at N = 50, about
power, or about `max_of_n`'s behaviour on rotation nulls. Those need their own
measurements and this document does not claim them.

## 5. Running it

    python docs/reviews/task65/centring.py

Results to `docs/reviews/task65/centring.json`.
