## ADDED Requirements

### Requirement: Stable-reference index follows every mutation path
Writer hooks, moves, deletes, watcher events, and reconcile SHALL keep the reference index aligned with governed Markdown. Missed events SHALL be detectable as audit drift and repairable by reconcile or full rebuild.

#### Scenario: External rename heals reference path
- **WHEN** a governed page with an `exomem_id` is renamed outside Exomem and reconcile runs
- **THEN** the canonical reference resolves to the renamed path and the stale mapping is removed

### Requirement: Reference drift is explicit
Audit SHALL report missing mappings, stale paths, duplicate IDs, malformed IDs, and sidecar rows for missing files without silently selecting a duplicate.

#### Scenario: Duplicate IDs refuse ambiguous resolution
- **WHEN** two governed pages contain the same `exomem_id`
- **THEN** audit reports both paths and canonical resolution fails with a stable ambiguity error
