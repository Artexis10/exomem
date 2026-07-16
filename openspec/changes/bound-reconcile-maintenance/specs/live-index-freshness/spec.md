## MODIFIED Requirements

### Requirement: Freshness Reconciliation Bounds Missed Events

The system SHALL bound how stale the event-maintained freshness registry can become from a missed filesystem event by periodically re-walking each live scope's tree and reconciling the registry against the fresh walk's result, on an interval independent of file-change events. A mismatch between an existing registry baseline and the fresh walk MUST be logged, MUST be corrected in the registry, and MUST return only the exact changed/deleted delta for derived-index fanout. When no prior baseline exists for a scope, the fresh walk SHALL install the initial baseline without reporting drift or dispatching the current corpus as changed; a previously seeded empty map remains an existing baseline. A successful user-invoked write-mode reconcile SHALL install final on-disk freshness baselines before returning, while event-maintained indexes are enabled, instead of leaving the registry non-live until the periodic timer.

#### Scenario: Periodic reconciliation heals a missed event

- **WHEN** a filesystem change event for a live-registry scope is missed and the periodic reconciliation interval elapses
- **THEN** the registry is re-walked and corrected to match the on-disk tree
- **AND** the mismatch is logged
- **AND** only the exact changed or deleted paths are returned for fanout

#### Scenario: Missing baseline initializes without phantom drift

- **WHEN** periodic reconciliation runs for a scope whose registry has no prior baseline
- **THEN** the fresh walk is installed as that scope's live baseline
- **AND** no current path is reported or dispatched as changed

#### Scenario: Seeded empty baseline still detects a new file

- **WHEN** a scope was explicitly seeded with an empty baseline
- **AND** a Markdown file appears before periodic reconciliation
- **THEN** that file is reported exactly once as changed

#### Scenario: Explicit reconcile leaves exact live baselines

- **WHEN** a user-invoked write-mode reconcile completes successfully with event-maintained indexes enabled
- **THEN** the `kb` and `vault` freshness scopes are live and match final on-disk state before the command returns
- **AND** the next unchanged watcher reconciliation produces no index fanout or deferred semantic receipts

#### Scenario: Real change after rebaseline still dispatches

- **WHEN** explicit reconcile installs final freshness baselines
- **AND** a Markdown source is subsequently modified or deleted
- **THEN** watcher reconciliation reports and dispatches that exact path once

#### Scenario: Scope baselines are independent

- **WHEN** one freshness scope has a prior baseline and another scope does not
- **THEN** the missing scope initializes without drift
- **AND** real drift in the existing scope is still reported and dispatched
