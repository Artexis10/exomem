# targets-report-state-verdicts

## Why

Incident, 2026-11-11: after a host restart the managed personal service failed
hundreds of starts. The worker at 0.171.0 had recorded the optional state
format `collections-store-schema-17` in the vault's state migration manifest.
The launcher venv still ran 0.168.0, whose closed optional-format catalog does
not know that id, so `partition_state_descriptor_ids` classified it as a
physical family and manifest validation raised `complete manifest has
incomplete families` inside `Supervisor.start()`. The supervisor exited on
every start until an offline launcher upgrade.

The launcher's manifest read is advisory: it decides whether a handoff needs a
state migration and whether a candidate supports the vault's recorded formats.
Enforcement that a release never opens state newer than itself already lives in
the worker's own startup gate. A supervisor that interprets format ids with its
own catalog is the trap: the launcher is precisely the component a worker
upgrade leaves behind, so the first format id written after it crashes the
whole service, including the supervisor's own status and control surfaces.

## What Changes

- The supervisor stops interpreting the state manifest's descriptor content
  with its own release's format catalog. The state verdict — serve, needs a
  state migration, or cannot serve — is computed by the target release's own
  code, probed under the target's interpreter, beside the existing inspect
  probe.
- `migration_required` consumes the target-reported verdict. A target that
  reports it cannot serve the vault's state is refused exactly where the
  launcher's own `required - supported` check refuses today.
- A target too old to report keeps today's launcher-side computation as the
  fallback. When neither side produces a verdict, a handoff still declares the
  migration (the target's own migrator decides under its interpreter) and a
  first start still spawns the recorded active worker (the worker's own gate
  decides). The launcher never crash-loops on a format id again.
- `upgrade_blocker` is unchanged: a supervisor that cannot read the vault's
  manifest still refuses to orchestrate upgrades over that state. Serving is
  not orchestrating.
- `docs/managed-service-upgrades.md` gains the new skew behaviour.

## Capabilities

### Modified Capabilities

- `managed-service-upgrades`: state verdicts for start and handoff are computed
  under the target interpreter; the supervisor compares reported sets and never
  parses optional format ids itself.

## Impact

Code: `src/exomem/service_manager.py` (verdict probe, `_state_migration_required`,
`migration_required`) and `src/exomem/service_upgrade.py` only if the staging
probe shares the new helper. Tests: service-manager tests covering a manifest
with an optional id the launcher's catalog does not know, an old target without
the verdict probe, and a self-reported cannot-serve verdict at first start.
Docs: `docs/managed-service-upgrades.md`. No deployment, schema or migration
impact: the probe is read-only and the fallback preserves every current
verdict.
