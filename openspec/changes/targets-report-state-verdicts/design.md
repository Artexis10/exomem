# Design

## Decision

Who computes "can this target serve this vault's state, and does the state need
a migration"? Today: the supervisor, with its own installed format catalogs.
After this change: the target release, with its own installed catalogs, under
the target's interpreter.

## Mechanism

`inspect()` already runs a bounded probe under `target["python"]` that reports
the candidate's protocol, declared descriptors, supported optional formats and
store schema ceiling. This change adds a `state_verdict` field to that probe:
the script imports a new `exomem.service_manager.vault_state_verdict(vault)`
under the target interpreter and prints its dict as JSON. Riding inside
`inspect` binds the verdict to the same verified candidate tuple as the other
declarations and keeps `migration_required` synchronous; a separate probe
could not race a state change and would need an async seam. A target whose
release predates the function reports `None` under the probe's own
`try/except`, exactly as every other optional probe field degrades.

`vault_state_verdict` mirrors `_state_migration_required` one-for-one using the
running release's own catalogs, plus one check the launcher cannot express:
`unrecognized_compatibility_ids` under the *target's* catalog. It returns:

- `{"verdict": "serve"}` — no refusal, no migration.
- `{"verdict": "migrate", "reason": <today's reason strings>}` — serve after
  the migrator runs.
- `{"verdict": "refuse", "reason": <why>}` — the target cannot serve this
  state (unrecognized recorded format, required compatibility it lacks, a live
  store schema above its ceiling, or a manifest it cannot parse).

The supervisor maps `refuse` to the existing `ValueError` refusal — a clean
`{"ok": false}` on upgrade, a failed exit status on first start — and `migrate`
to `(True, reason)`. Unrecognized-id refusal by a *newer* target never happens:
a newer target recognizes every id the state records.

## Fallbacks

- **Target cannot report** (release predates the probe): the supervisor falls
  back to its own manifest read — today's computation, unchanged.
- **Neither side can read the state** (manifest newer than the launcher *and*
  target predates the probe): the verdict is unavailable. A handoff declares
  the migration — the migrator child already runs under the target interpreter
  and is the component that can read-or-refuse the state; a first start spawns
  the recorded active worker — the worker's own startup gate is the arbiter.
  Failure direction stays refuse-safe: an old worker that truly cannot serve
  exits and the supervisor stops with a failed status, as the spec requires.
- **Corrupt manifest** (unreadable for reasons other than format skew):
  `unrecognized_compatibility_ids` tolerantly reports none, the target's own
  `_load_manifest` then raises, and the verdict reports `refuse`.

## What does not change

- `upgrade_blocker`: orchestrating a handoff keeps launcher-side state reads
  (`handoff_state`, collections migration staging), so a supervisor that cannot
  read the manifest still refuses upgrades with the documented maintenance
  path. Serving the recorded active worker does not orchestrate.
- In-place worker restart (`restart_worker`): it never read the manifest.
- The worker's own startup gate: unchanged enforcement.
- The inspect probe's bounds and protocol version: the verdict walk is a few
  local manifest reads and one store metadata read inside the same bounded
  probe, so it cannot starve the inspect budget.

## Rejected alternatives

- **Refuse the upgrade preemptively** when a candidate can write ids the
  launcher predates: every format bump becomes a mandatory offline launcher
  outage, and the already-written case still crashes at startup.
- **Upgrade the launcher venv with the worker**: the running supervisor keeps
  its in-memory catalog until a unit restart, so the trap survives until the
  next restart, which is exactly the event that crashed.
