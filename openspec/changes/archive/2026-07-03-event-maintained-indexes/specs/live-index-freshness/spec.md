## MODIFIED Requirements

### Requirement: External File Edits Still Reindex Live

The system SHALL continue to reindex out-of-band markdown edits observed by the live watcher,
including edits from Obsidian, mobile sync, manual filesystem writes, and git updates. Self-write
suppression MUST NOT disable the existing debounce, batching, upsert, or delete behavior for events
that were not registered as Exomem-authored mutations. The watcher observes changes across the
whole vault root, not only `Knowledge Base/`, but embedding reindex dispatch (`upsert_after_write`/
`delete_after_remove`) MUST remain scoped to markdown under `Knowledge Base/` exactly as before —
vault-root markdown outside `Knowledge Base/` is observed for freshness and inbound-link
maintenance (see the event-maintained requirements below) but MUST NOT trigger an embedding upsert
or delete, since it was never embedded in the first place.

#### Scenario: Manual markdown edit still upserts

- **WHEN** a markdown file under `Knowledge Base/` is modified outside an Exomem writer path
- **THEN** the watcher debounces the event and calls `embeddings.upsert_after_write` for that file

#### Scenario: Manual markdown delete still deletes sidecar rows

- **WHEN** a markdown file under `Knowledge Base/` is deleted outside an Exomem writer path
- **THEN** the watcher debounces the event and calls `embeddings.delete_after_remove` with the
  vault-relative path

#### Scenario: Vault-root edit outside KB updates freshness without embedding reindex

- **WHEN** a markdown file outside `Knowledge Base/` but inside the vault root is created, modified,
  or deleted
- **THEN** the watcher observes the event and updates the vault-scope freshness and inbound-link
  registries for that path
- **AND** the watcher does not call `embeddings.upsert_after_write` or `embeddings.delete_after_remove`
  for that path

## ADDED Requirements

### Requirement: Event-Maintained Markdown Freshness Keys

The system SHALL maintain, in memory, a per-scope registry of `{vault-relative path: mtime}` for
markdown under each freshness scope (`kb`, `vault`), updated from the live file watcher and from
in-process writer paths as they mutate the vault, so that a live registry can answer a scope's
freshness triple (file count, max mtime, digest) without a filesystem walk. The registry MUST apply
the identical inclusion rules the walk it replaces would apply (the same skip-directories, the same
`.md`-only filter, the same scope tree roots), so that the triple derived from the registry is
identical to the triple a fresh walk of the same tree would produce. When the registry for a scope
is not live (never seeded, or event-maintained indexes are disabled), consumers MUST fall back to
performing the walk exactly as before this capability existed. A rename MUST change the scope's
digest even when the renamed file's mtime is unchanged, because the digest is derived over
vault-relative paths as well as mtimes. A write performed by an Exomem writer whose watcher echo is
suppressed for embedding-reindex purposes MUST still update the freshness registry for the written
or removed path.

#### Scenario: Live registry answers freshness without a walk

- **WHEN** the freshness registry for a scope is live and a caller requests that scope's freshness
  triple
- **THEN** the triple is derived from the in-memory map with no filesystem walk
- **AND** the triple is identical to the triple a fresh walk of the same tree would produce

#### Scenario: Not-live registry falls back to a walk

- **WHEN** the freshness registry for a scope has never been seeded, or event-maintained indexes are
  disabled
- **THEN** the freshness triple for that scope is computed by walking the tree, exactly as before
  this capability existed

#### Scenario: A create, modify, delete, or move updates the registry

- **WHEN** a markdown file within a scope's tree is created, modified, deleted, or moved
- **THEN** the scope's registry reflects the change (the path's presence and mtime, or its absence
  for a delete) without requiring a fresh walk to observe it

#### Scenario: A rename with a preserved mtime still changes the digest

- **WHEN** a markdown file is renamed such that its mtime is unchanged by the rename
- **THEN** the scope's registry-derived digest changes, because the digest is derived over
  vault-relative paths as well as mtimes

#### Scenario: A suppressed self-write still updates freshness

- **WHEN** an Exomem writer performs a markdown mutation whose watcher echo is suppressed to avoid a
  duplicate embedding reindex
- **THEN** the freshness registry for the affected scope(s) is still updated to reflect the
  mutation, independent of the embedding-reindex suppression

#### Scenario: Event-maintained indexes can be disabled wholesale

- **WHEN** the server runs with event-maintained indexes disabled
- **THEN** the freshness registry is never treated as live, and every freshness lookup falls back to
  the walk-based computation

### Requirement: Freshness Reconciliation Bounds Missed Events

The system SHALL bound how stale the event-maintained freshness registry can become from a missed
filesystem event by periodically re-walking each live scope's tree and reconciling the registry
against the fresh walk's result, on an interval independent of file-change events. A mismatch
between the registry and the fresh walk MUST be logged and MUST be corrected in the registry (the
fresh walk's result wins). A user-invoked reconcile operation MUST also invalidate the
event-maintained registries as part of its own end-of-run cleanup, in addition to the periodic
background reconciliation.

#### Scenario: Periodic reconciliation heals a missed event

- **WHEN** a filesystem change event for a live-registry scope is missed (not observed by the
  watcher) and the periodic reconciliation interval elapses
- **THEN** the registry is re-walked and corrected to match the on-disk tree
- **AND** the mismatch is logged

#### Scenario: A user-invoked reconcile invalidates the registries

- **WHEN** a user-invoked reconcile operation completes
- **THEN** the freshness, matrix-sharing, and inbound-link registries are invalidated as part of
  that operation's cleanup, independent of the periodic reconciliation timer

### Requirement: Event-Maintained Inbound-Link Index

The system SHALL maintain the inbound wikilink index incrementally: when a specific set of markdown
files changes, the system SHALL update only the affected files' entries in the index (removing their
prior contributions and re-reading only those files) rather than re-scanning the entire vault. The
resulting index's content (which inbound links exist for a given target) MUST be identical to what a
full rebuild of the index would produce for the same vault state. When the incremental registry is
not live, the system MUST fall back to the existing full-vault rebuild.

#### Scenario: A single-file change patches only that file's entries

- **WHEN** one markdown file changes and the inbound-link index is notified of that change
- **THEN** only that file's prior wikilink entries and basename-count contribution are removed and
  recomputed
- **AND** no other file is re-read

#### Scenario: A patched index matches a full rebuild in content

- **WHEN** the same sequence of file changes is applied once via incremental patching and once via a
  full rebuild from the resulting vault state
- **THEN** the set of inbound links returned for any given target is identical between the two

#### Scenario: A rename is reflected without a full rescan

- **WHEN** a markdown file referenced by wikilinks is renamed and the inbound-link index is notified
- **THEN** a subsequent inbound-link lookup for the old and new paths reflects the rename without a
  full-vault rescan

#### Scenario: Not-live index falls back to a full rebuild

- **WHEN** the incremental inbound-link registry is not live
- **THEN** the inbound-link index is computed by a full-vault rebuild, exactly as before this
  capability existed
