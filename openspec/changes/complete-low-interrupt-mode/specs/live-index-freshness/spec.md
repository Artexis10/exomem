## MODIFIED Requirements

### Requirement: External File Edits Still Reindex Live

The system SHALL continue to observe out-of-band markdown edits through the live
watcher, including edits from Obsidian, mobile sync, manual filesystem writes,
and git updates. Self-write suppression MUST NOT disable debounce, batching, or
freshness/inbound maintenance for events that were not registered as
Exomem-authored mutations. The watcher observes changes across the whole vault
root, not only `Knowledge Base/`, but embedding reindex dispatch
(`upsert_after_write`/`delete_after_remove`) MUST remain scoped to markdown under
`Knowledge Base/` exactly as before. In `quiet` mode, expensive embedding and
CLIP reindex work MAY be deferred or capped instead of running immediately, but
cheap freshness and inbound-link maintenance MUST still reflect the observed
filesystem change.

#### Scenario: Manual markdown edit still upserts outside quiet mode

- **WHEN** a markdown file under `Knowledge Base/` is modified outside an Exomem
  writer path
- **AND** the active resource policy does not defer expensive index work
- **THEN** the watcher debounces the event and calls the embedding upsert path
  for that file

#### Scenario: Quiet manual markdown edit updates freshness and defers expensive work

- **WHEN** a markdown file under `Knowledge Base/` is modified outside an Exomem
  writer path
- **AND** the effective mode is `quiet`
- **THEN** the watcher updates freshness, inbound-link, and resolver state for
  that file
- **AND** the watcher records the expensive semantic or visual reindex work as
  deferred rather than forcing immediate embedding work

#### Scenario: Manual markdown delete still removes sidecar rows outside quiet mode

- **WHEN** a markdown file under `Knowledge Base/` is deleted outside an Exomem
  writer path
- **AND** the active resource policy does not defer expensive index work
- **THEN** the watcher debounces the event and calls the embedding delete path
  with the vault-relative path

#### Scenario: Vault-root edit outside KB updates freshness without embedding reindex

- **WHEN** a markdown file outside `Knowledge Base/` but inside the vault root is
  created, modified, or deleted
- **THEN** the watcher observes the event and updates the vault-scope freshness
  and inbound-link registries for that path
- **AND** the watcher does not call embedding upsert or delete for that path

## ADDED Requirements

### Requirement: Quiet Mode Throttles Watcher And Reconcile Work

The system SHALL make watcher dispatch and periodic reconcile mode-aware. In
quiet mode the watcher SHALL use a low-interrupt policy that coalesces filesystem
bursts more aggressively, caps expensive per-cycle indexing work, and avoids
materializing large warm caches solely as a side effect of reconcile. The policy
MUST be read at runtime so switching modes does not require restarting the
server.

#### Scenario: Quiet watcher coalesces bursts

- **WHEN** the effective mode is `quiet`
- **AND** multiple filesystem events arrive in a short burst
- **THEN** the watcher waits for the quiet-mode debounce window and dispatches
  one coalesced batch rather than one expensive operation per event

#### Scenario: Quiet reconcile caps expensive reindex

- **WHEN** periodic reconcile detects a large drift while the effective mode is
  `quiet`
- **THEN** freshness registries are corrected to match disk
- **AND** expensive semantic or visual reindex work is capped or deferred
- **AND** the cap or deferral is recorded for status or logs

#### Scenario: Mode switch changes watcher policy without restart

- **WHEN** the server is running and the effective mode changes from `quiet` to
  `normal`
- **THEN** the next watcher or reconcile cycle uses the normal-mode policy
  without requiring a server restart

### Requirement: Deferred Expensive Index Work Is Observable And Healable

The system SHALL track expensive index work deferred by quiet mode. Deferred work
SHALL be visible through resource status or logs, and it SHALL be healable by
leaving quiet mode, running the explicit indexing command, or running explicit
reconcile. Deferred work MUST NOT hide cheap freshness updates: keyword, BM25,
and graph lanes SHALL see current markdown state when their underlying indexes
can be updated cheaply.

#### Scenario: Deferred work appears in status

- **WHEN** quiet mode defers semantic or visual indexing work
- **THEN** resource status reports that deferred expensive index work exists
- **AND** it includes a best-effort count or summary of the pending paths

#### Scenario: Leaving quiet can flush deferred work

- **WHEN** deferred expensive index work exists
- **AND** the effective mode changes from `quiet` to `normal` or `performance`
- **THEN** Exomem may process the deferred work in the background according to
  the new mode's device and batching policy

#### Scenario: Explicit index heals deferred semantic work

- **WHEN** deferred semantic index work exists
- **AND** the user runs the explicit indexing command for the affected scope
- **THEN** the command processes the changed files and clears the corresponding
  deferred semantic work record
