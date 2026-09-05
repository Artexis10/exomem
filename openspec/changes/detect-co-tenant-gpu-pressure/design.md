# Design — co-tenant GPU pressure

## Attribution is the core requirement

`--query-gpu=utilization.gpu` returns one number for the whole device with no
ownership. `nvidia-smi --query-compute-apps=pid,used_memory` attributes VRAM
per process, which is what lets Exomem subtract itself before deciding whether
a co-tenant needs the card.

Two candidate mechanisms, to be settled during implementation:

1. **Subtract our own PID** from the compute-apps listing. Direct, but
   `--query-compute-apps` was measured (2026-09-04, this machine) to list only
   graphics apps with `[N/A]` memory under WSL, so it may not attribute WSL
   CUDA usage at all. Verify before relying on it.
2. **Suppress the utilisation arm while this process holds a CUDA context.**
   Weaker but robust: if Exomem is itself on the GPU, utilisation cannot be
   read as a clean co-tenant signal, so fall back to the VRAM arm alone.

Option 2 is the safe default if option 1 cannot be verified on the target
platform. Either way the invariant is the same: **a mode restore must never
re-arm the trigger that caused the demotion.**

## Thresholds must bound against real need, not card size

The rejected attempt scaled the pressure floor as 25% of total VRAM. Exomem's
actual placement need is a constant 2048 MB, so the gap grew without bound: on
an 80 GB card the floor became 20389 MB, declaring pressure with 20 GB free —
about ten times what Exomem needs to run. At the other end, a 2 GB card gets a
floor equal to 100% of its VRAM, making `free_mb < min_free` unsatisfiable and
latching auto-quiet into permanent quiet.

Any floor must therefore be bounded above by Exomem's real need plus a fixed
headroom, and clamped strictly below `total_mb`.

## An unreadable signal is not a quiet card

The rejected attempt returned `utilization_pct: None` on an unparseable field
and then treated `None` as "no pressure", emitting `status: capable,
reason: None` — byte-identical to a genuinely idle card. Two reachable inputs
produce it: a driver reporting `[N/A]`, and a GPU name containing a comma
(the CSV is unquoted, so `name` before `utilization.gpu` shifts every later
field). Put `name` last in the query, and surface an unread signal explicitly.

## Open questions

- Does `--query-compute-apps` attribute CUDA usage under WSL at all? Measured
  once as listing only graphics apps with `[N/A]` memory. Decides mechanism 1
  vs 2.
- Does `utilization.gpu` inside WSL actually move when a Windows game runs?
  Never verified with a game running; the card read 1-5% throughout. If it
  does not track host load, the whole utilisation arm is the wrong sensor and
  this change needs a different signal.
- Multi-GPU hosts: `gpu_headroom()` reads `splitlines()[0]` only. A busy GPU 0
  beside an idle GPU 1 yields `marginal` for the host. In scope for this
  product or not?
