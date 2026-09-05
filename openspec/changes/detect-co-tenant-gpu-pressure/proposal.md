# Detect co-tenant GPU pressure

## Why

Auto-quiet exists so Exomem yields the machine when something else needs the
GPU. It cannot currently see the most common case.

`resource_status.gpu_headroom()` queries only `memory.free,memory.total,name`.
A game that has finished allocating its buffers sits at steady VRAM while
pinning the shaders, so free VRAM alone reads that as a quiet card.
`auto_quiet.pressure_active()` maps `marginal` to `True`, so auto-quiet sleeps
through exactly the workload it exists to yield to.

The operator's requirement, in their words: *"I just hope that somehow I can
have WSL on and then game and have it all coexist, not mutually exclusive."*

## What a naive fix gets wrong

A first attempt (PR #1062, rejected on review) added `utilization.gpu` and
reported `marginal` at or above 90%. That is not sufficient, because
utilisation does not say *whose* work it is:

- `accel.py` sets `want_cuda=(m == "performance")`, so in performance mode
  Exomem's own embedding and ASR batches pin the card.
- `auto_quiet.decide()` restores `previous_mode`, which is `performance`.

The two compose into a self-sustaining limit cycle: pressure → demote to quiet
→ Exomem's GPU work stops → pressure clears → restore to performance → work
resumes → pressure. Roughly 100 seconds per cycle, rewriting the user's config
file on disk twice each time and unloading/reloading every model singleton.
Performance mode becomes unavailable most of the time, for precisely the users
who opted into both features.

The lesson is that a pressure signal must be attributable. "The GPU is busy" is
not a reason to yield if Exomem is what is keeping it busy.

## What changes

Pressure detection distinguishes co-tenant load from Exomem's own, and reports
an unreadable signal as unreadable rather than as absence of pressure.

## Non-goals

- Changing device placement. `accel.gpu_usable()` and
  `EXOMEM_GPU_MIN_FREE_GB` keep their current meaning and threshold; this is
  about when to yield, not where to put a model.
- Multi-GPU scheduling. Reading only the first device is a known present
  limitation and is recorded as an open question, not solved here.
