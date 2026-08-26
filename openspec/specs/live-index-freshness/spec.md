# live-index-freshness Specification

## Purpose
Keep `find` latency and reindex work from scaling with vault size and write
volume: event-maintained in-memory registries for markdown freshness and
inbound wikilinks are updated incrementally from the live file watcher and
in-process writers, so most freshness checks and link lookups need no
filesystem walk or full-vault rescan. Self-authored writer mutations are
suppressed from re-triggering a duplicate embedding reindex through the
watcher, while external edits (Obsidian, sync, git) still reindex live, and
periodic reconciliation bounds how stale a registry can drift from a missed
event. Every event-maintained path falls back to the prior walk/rescan
behavior when the registry isn't live.
## Requirements
### Requirement: Suppress Exomem Self-Write Watcher Echo

The system SHALL suppress live file-watcher events that correspond to filesystem mutations already
performed by Exomem writer paths and already handled by the writer embedding hooks. A suppressed
self-write event MUST NOT trigger a duplicate `embeddings.upsert_after_write` or
`embeddings.delete_after_remove` call from the watcher. Suppression MUST be bounded and keyed to the
same vault-relative path that the watcher would otherwise enqueue.

#### Scenario: Batch write does not reindex twice

- **WHEN** an Exomem writer updates markdown through the normal atomic batch-write path
- **AND** that writer has already refreshed embeddings for the written markdown
- **THEN** the watcher does not enqueue a second upsert for the same self-authored filesystem event

#### Scenario: Self-authored delete does not delete twice

- **WHEN** an Exomem writer removes, trashes, or moves a markdown file and already updates the
  embedding sidecar for the removed path
- **THEN** the watcher does not enqueue a duplicate delete for the same self-authored filesystem
  event

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

#### Scenario: Manual markdown edit still upserts

- **WHEN** a markdown file under `Knowledge Base/` is modified outside an Exomem writer path
- **THEN** the watcher debounces the event and calls `embeddings.upsert_after_write` for that file

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

#### Scenario: Manual markdown delete still deletes sidecar rows

- **WHEN** a markdown file under `Knowledge Base/` is deleted outside an Exomem writer path
- **THEN** the watcher debounces the event and calls `embeddings.delete_after_remove` with the
  vault-relative path

#### Scenario: Vault-root edit outside KB updates freshness without embedding reindex

- **WHEN** a markdown file outside `Knowledge Base/` but inside the vault root is
  created, modified, or deleted
- **THEN** the watcher observes the event and updates the vault-scope freshness
  and inbound-link registries for that path
- **AND** the watcher does not call embedding upsert or delete for that path

### Requirement: Self-Write Suppression Cannot Hide Later External Edits

The system SHALL make self-write suppression temporary and freshness-aware. For create/modify
events, suppression MUST match the self-authored file signature, such as mtime plus size, before the
watcher drops an event. A later external edit to the same path MUST be treated as a normal watcher
event once the signature changes or the suppression entry expires.

#### Scenario: Later edit to same path reindexes

- **WHEN** Exomem writes a markdown file and registers the self-write for watcher suppression
- **AND** a later external edit changes that same file
- **THEN** the watcher treats the later event as external
- **AND** the watcher enqueues an embedding upsert for the edited file

#### Scenario: Suppression entries expire

- **WHEN** a self-write suppression entry is older than its bounded lifetime
- **THEN** the watcher no longer uses that entry to drop events

### Requirement: Event-Maintained Markdown Freshness Keys

The system SHALL maintain, in memory, a per-scope registry of policy-admitted `{path: signature}` rows for markdown under each recall scope (`kb`, `vault`), updated from a safely enumerated seed, the live file watcher, and in-process writer events. A live registry SHALL answer a scope's freshness triple and exact allowed-path projection without a request-time filesystem walk. Seed and reconcile SHALL publish a complete replacement map and its checkpoint atomically; readers SHALL continue observing the last proven map until the replacement is authoritative. Observation events received before initial seed publication SHALL be retained and replayed against the published generation. Event-derived paths SHALL retain Windows long-name canonicalisation, reparse/no-follow validation, access-policy checks, and Records/Planning admission before publication.

Activated server consumers MUST NOT fall back to a walk when a registry is not live; they SHALL report retrieval warming/unavailable and allow background recovery to establish authority. Explicit offline callers and deployments with event indexes disabled MAY use the prior walk fallback. A rename MUST change the scope digest even when mtime is preserved, and a suppressed self-write MUST still update every affected live projection.

#### Scenario: Live registry answers freshness without a walk

- **WHEN** the recall registry for a scope is live and a caller requests its checkpoint and allowed paths
- **THEN** both are copied from one authoritative in-memory generation with no filesystem walk
- **AND** they equal a fresh policy-projected walk over the same state

#### Scenario: Server with a not-live registry declines without walking

- **WHEN** an activated server request needs a scope whose recall registry is not live
- **THEN** the request receives an explicit warming or unavailable outcome
- **AND** the request does not walk the scope

#### Scenario: Offline caller retains the walk fallback

- **WHEN** an explicit offline caller has no live registry, or event-maintained indexes are disabled
- **THEN** the caller may compute the projection by walking the source tree
- **AND** the same admission and access policy is applied

#### Scenario: Not-live registry falls back to a walk

- **WHEN** an explicit offline caller's freshness registry has never been seeded, or a deployment runs with event-maintained indexes disabled
- **THEN** the freshness triple for that scope is computed by walking the tree exactly as before this capability existed
- **AND** an activated managed server with event-maintained indexes enabled still declines instead of taking this fallback

#### Scenario: Reconcile replacement is atomic

- **WHEN** periodic reconciliation derives a replacement projection while readers are active
- **THEN** readers observe either the complete previous checkpoint/map or the complete replacement checkpoint/map
- **AND** no reader observes a mixed or empty intermediate generation

#### Scenario: Startup event survives seed replacement

- **WHEN** a create, modify, delete, or move is observed after enumeration begins but before initial replacement publication
- **THEN** the event remains buffered until the replacement is authoritative
- **AND** applying the event advances the resulting live generation before consumers rely on it

#### Scenario: Event path aliases are validated once before publication

- **WHEN** Windows reports a changed file through an 8.3, case, or equivalent alias spelling
- **THEN** event ingress canonicalises and validates that changed identity before publishing it
- **AND** the alias cannot bypass Records/Planning suppression or the vault boundary

#### Scenario: A create, modify, delete, or move updates the registry

- **WHEN** an admitted markdown identity is created, modified, deleted, or moved through an external event
- **THEN** every affected live scope advances to a checkpoint containing that change without a full re-seed

#### Scenario: A rename with a preserved mtime still changes the digest

- **WHEN** an admitted markdown identity is renamed without changing its mtime
- **THEN** every affected registry-derived digest changes because the digest includes the canonical relative path

#### Scenario: A suppressed self-write still updates freshness

- **WHEN** an Exomem writer performs a markdown mutation whose watcher echo is suppressed to avoid duplicate embedding work
- **THEN** every affected live projection advances to a checkpoint containing that mutation independently of watcher suppression

#### Scenario: Event-maintained indexes can be disabled wholesale

- **WHEN** the server runs with event-maintained indexes explicitly disabled
- **THEN** no recall registry is treated as live
- **AND** the declared legacy walk-backed fallback remains available

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

### Requirement: Lexical Index Synchronization

The system SHALL keep the lexical sidecar synchronized with the vault's markdown
through the same freshness seams that maintain the embedding sidecars — in-process
writer hooks, the live file watcher, and reconcile — on lean installs as well as
full ones (lexical maintenance MUST NOT be gated behind the embeddings extra).
When the sidecar is missing, stale, or was written past by a non-aware process,
the next use MUST detect the mismatch (page count and max mtime against the
markdown walk) and rebuild the affected state from the markdown source of truth
without user action. The `find` hot-cache freshness key MUST incorporate the
lexical sidecar's freshness so cached results cannot outlive a lexical reindex.

#### Scenario: A write keeps the lexical index current

- **WHEN** a markdown page is created, edited, or deleted through a writer path
  or observed by the watcher
- **THEN** the lexical sidecar reflects the change through the same seam that
  refreshes the embedding sidecars
- **AND** a subsequent bm25- or keyword-lane query observes the change

#### Scenario: A pre-existing vault is indexed on first use

- **WHEN** a vault that predates the lexical sidecar is first used by an aware
  version
- **THEN** the sidecar is created and populated from the markdown walk
- **AND** no user action is required

#### Scenario: Lean installs maintain the lexical index

- **WHEN** the server runs a lean install (no embeddings extra)
- **THEN** writer and watcher events still keep the lexical sidecar current

#### Scenario: Out-of-band drift self-heals

- **WHEN** markdown changed without the lexical sidecar being updated
- **THEN** the next use detects the count/mtime mismatch and rebuilds the
  affected state from markdown

### Requirement: Graph Sidecar Incremental Freshness
The system SHALL maintain graph sidecar freshness from the same writer and watcher event streams that maintain other derived indexes. When a Markdown file changes, the graph index SHALL update or remove only graph rows contributed by the affected path when possible. The resulting graph SHALL be equivalent to a full rebuild over the same vault state.

#### Scenario: Single-file edit updates only affected graph rows
- **WHEN** one governed Markdown file changes and the graph index is notified
- **THEN** graph nodes and edges contributed by that file are refreshed
- **AND** unchanged files do not need to be re-read for graph rows unrelated to the changed file

#### Scenario: Incremental graph matches full rebuild
- **WHEN** the same sequence of Markdown changes is applied once through incremental graph updates and once through a full graph rebuild
- **THEN** graph context for any affected seed returns equivalent nodes, edges, and provenance

### Requirement: Graph Drift Is Auditable And Reconciled
The system SHALL detect graph sidecar drift caused by missing rows, stale source hashes, schema mismatch, or a missing sidecar. Audit/reconcile SHALL surface and repair graph drift without mutating canonical Markdown content. When graph indexing is disabled, graph drift checks SHALL short-circuit cleanly.

#### Scenario: Reconcile rebuilds stale graph rows
- **WHEN** graph audit detects that graph rows for a Markdown file are stale relative to the file's current content
- **THEN** reconcile refreshes the graph rows for that file or rebuilds the graph sidecar
- **AND** the source Markdown file remains unchanged

#### Scenario: Disabled graph indexing is a no-op
- **WHEN** graph indexing is disabled
- **THEN** graph drift checks return no actionable findings
- **AND** no optional graph dependency or sidecar is required

### Requirement: Graph Freshness Cannot Hide External Edits
Self-write suppression for Exomem-authored filesystem events SHALL NOT hide later external edits from graph maintenance. A later edit, delete, or move observed from Obsidian, mobile sync, manual filesystem changes, or git operations SHALL update graph freshness or be repaired by reconcile.

#### Scenario: External edit after self-write refreshes graph
- **WHEN** Exomem writes a note and suppresses its own watcher echo
- **AND** a later external edit changes that same note
- **THEN** graph freshness treats the later event as external
- **AND** the graph rows for the edited note are refreshed or marked stale for reconcile

#### Scenario: Missed graph event is healed
- **WHEN** a filesystem event that should refresh graph rows is missed
- **THEN** periodic or user-invoked reconcile detects the graph freshness mismatch
- **AND** the graph sidecar is corrected to match the on-disk Markdown state

### Requirement: Bounded live semantic encoding
Live semantic indexing SHALL split work by a configurable maximum chunk count. A single encode
call MUST NOT receive more than that bound, including when one document alone exceeds it, and
all chunks SHALL be committed under the existing file identity contract.

#### Scenario: Large import batch
- **WHEN** changed files produce more chunks than the live encode bound
- **THEN** embedding runs as multiple bounded encode calls
- **AND** every eligible file is indexed without one unbounded flattened allocation

#### Scenario: One oversized document
- **WHEN** one document produces more chunks than the live encode bound
- **THEN** its chunks are encoded in bounded slices
- **AND** the final file rows contain all slices in original order

### Requirement: Deferred semantic work survives restart
Deferred semantic paths SHALL be stored in a rebuildable per-vault SQLite sidecar, deduplicated,
visible through resource status, and removed only after successful dispatch or explicit healing.

#### Scenario: Restart with deferred paths
- **WHEN** the server restarts after an import was deferred
- **THEN** resource status still reports the deferred paths
- **AND** an explicit index/reconcile can clear them after processing

### Requirement: Relation registry changes invalidate derived graph resolution
The graph sidecar SHALL record core registry version and extension-registry
content hash. A mismatch SHALL be detectable as graph drift and SHALL cause the
next explicit rebuild/reconcile path to re-resolve every edge deterministically.
Registry invalidation MUST NOT modify Markdown.

#### Scenario: Alias change rebuilds canonical edge identity
- **WHEN** a valid extension registry alias changes and reconcile runs
- **THEN** the graph sidecar is rebuilt against the new registry hash, raw
  observations remain unchanged, and canonical relation metadata reflects the
  new valid resolution

### Requirement: Traversal profile changes invalidate context plans only
The system SHALL hash governed traversal profiles for cache freshness. A
profile-only change SHALL invalidate cached profile/context plans but MUST NOT
force graph edge reindexing because stored edge resolution is unchanged.

#### Scenario: Profile edit avoids unnecessary graph rebuild
- **WHEN** a valid custom profile changes its included relation families
- **THEN** the next context call uses the new profile and no Markdown or graph
  edge rows are rewritten solely because of that profile edit

### Requirement: Reconcile Refreshes Complete Navigation Counts

The reconcile operation SHALL recompute Sources, Notes, and Entities totals and known per-type counts from disk. It SHALL update the top-level index and each existing Sources, Notes, and Entities sub-index while preserving curated descriptions and recent-activity content.

#### Scenario: Sources index drift is reconciled

- **WHEN** source files were added or removed outside Exomem and `reconcile` runs
- **THEN** `Sources/index.md` by-type counts and the top-level Sources total match on-disk source files

#### Scenario: Top index exposes real totals

- **WHEN** the vault contains notes and entities across several types
- **THEN** the top index reports total Notes and total Entities counts
- **AND** any retained per-type count rows match their corresponding on-disk types

### Requirement: Writers Insert Missing Total Count Rows

Normal governed writers that refresh navigation SHALL update existing count rows and SHALL insert missing `Sources`, `Notes`, or `Entities` total rows into a valid Counts section rather than silently leaving incomplete totals.

#### Scenario: Legacy scaffold has only subtype rows

- **WHEN** a legacy top index contains `Notes (insight)` and `Entities (concept)` but no total rows
- **AND** a governed writer refreshes counts
- **THEN** total Notes and Entities rows are inserted with correct on-disk totals
- **AND** the existing subtype rows remain accurate

### Requirement: Supported Media Events Dispatch Without Entering Text Freshness
The live watcher SHALL separately debounce supported media create/modify events under the governed Knowledge Base and dispatch them to canonical media reconciliation. Binary paths MUST NOT be passed to Markdown embedding upsert/delete or included in Markdown freshness and inbound-link registries.

#### Scenario: Audio event dispatches media only
- **WHEN** the watcher observes a new `.m4a` under the governed Knowledge Base
- **THEN** it dispatches targeted media reconciliation after the debounce window
- **AND** it does not pass the binary path to text embedding or Markdown freshness handlers

#### Scenario: Unsupported attachment remains ignored
- **WHEN** the watcher observes a non-Markdown attachment outside the supported media registry
- **THEN** neither media processing nor text-index dispatch occurs

### Requirement: Periodic Reconciliation Heals Media Event Drift
The existing periodic reconciliation loop SHALL run a bounded supported-media discovery pass independent of change events. The pass SHALL be idempotent and MUST NOT perform a full text-index rebuild solely because supported media exists.

#### Scenario: Periodic pass finds an unobserved recording
- **WHEN** a supported recording exists without canonical completed or pending state and no watcher event was observed
- **THEN** the next periodic pass creates or repairs the sidecar and durably enqueues processing
- **AND** unrelated Markdown files are not re-embedded

### Requirement: Freshness Registry Exposes Atomic Consumer Deltas

Each live scope registry SHALL have a process-instance ID and monotonic generation. A consumer checkpoint SHALL contain `{instance_id, generation, triple}`. An atomic delta request SHALL return `{from, to, complete, changed, deleted}` for one captured target generation, where `changed` and `deleted` are duplicate-free, mutually disjoint target-state path sets and `to` contains the exact target checkpoint. A path present at `to` SHALL appear only in `changed`; a path absent at `to` SHALL appear only in `deleted`, regardless of intermediate events. Multiple consumers MUST read deltas non-destructively.

#### Scenario: Edit and rename have exact representations

- **WHEN** one file is edited and another is renamed after a consumer checkpoint
- **THEN** a complete delta lists the edit and rename destination in `changed` and the rename source in `deleted`
- **AND** its `to` checkpoint identifies the exact snapshot containing those events

#### Scenario: Edit then delete coalesces to deletion

- **WHEN** one path is edited and then deleted between `from` and `to`
- **THEN** it appears only in `deleted`
- **AND** apply order cannot resurrect it

#### Scenario: Delete then recreate coalesces to change

- **WHEN** one path is deleted and recreated before `to`
- **THEN** it appears only in `changed`
- **AND** apply order cannot remove the recreated target state

### Requirement: Unknown Delta Never Returns A Partial Suffix

Process restart, reconciliation mismatch, retained-history overflow, a foreign instance ID, or a checkpoint older than retained history SHALL return `complete=false`. An incomplete response MUST NOT expose a retained suffix as if it were the full delta. A later event arriving after a captured `to` generation MUST remain discoverable from that `to` checkpoint.

#### Scenario: Overflow is explicitly incomplete

- **WHEN** event history overflows before a consumer requests its delta
- **THEN** the registry reports `complete=false`
- **AND** no consumer can advance its authoritative checkpoint from the incomplete response

#### Scenario: Concurrent event remains for the next delta

- **WHEN** an event arrives after `delta_since` captures its target generation but before the consumer commits repair
- **THEN** the current delta's `to` checkpoint remains unchanged
- **AND** requesting from that checkpoint returns the later event

### Requirement: Delta Application Advances Checkpoint Atomically

A sidecar consumer applying a complete delta SHALL commit all changed-path upserts, deleted-path removals, and the exact `to` checkpoint in one transaction. On rollback, neither rows nor checkpoint may advance.

#### Scenario: Failed patch cannot bless stale rows

- **WHEN** any path update fails while applying a complete delta
- **THEN** the transaction rolls back every row change and retains the prior checkpoint
- **AND** the next request still observes the delta as unapplied

### Requirement: Vector Table Synchronization

The system SHALL update the corresponding vec0 vector tables in the same transaction as
every sidecar write that mutates the embedding blob tables (`chunks` in
`.embeddings.sqlite`, `images` in `.clip.sqlite`), so a committed sidecar never exposes a
blob/vec row mismatch to readers in the writing process. When a sidecar was written without vec0
maintenance — a pre-existing sidecar from before this capability, or a writer process
where the extension is unavailable — the next vec-aware use MUST detect the mismatch and
rebuild the vec0 rows from the stored blobs without re-embedding and without user action.
Blob tables remain the source of truth: rebuilding vec0 rows MUST never invoke an
embedding model.

#### Scenario: A write keeps blob and vector tables in lockstep

- **WHEN** a file's rows are upserted or deleted through a sidecar writer with the vec0
  backend available
- **THEN** after the write commits, the vec0 table holds exactly one vector row per blob
  row
- **AND** a KNN query against the vec0 table reflects the write with no separate refresh
  step

#### Scenario: A pre-existing sidecar is migrated on first use

- **WHEN** a sidecar created before this capability (blob rows only, no vec0 tables) is
  first used by a vec-aware process
- **THEN** the vec0 tables are created and populated from the stored blobs
- **AND** no embedding model is loaded to do so

#### Scenario: Drift from a non-vec-aware writer self-heals

- **WHEN** a process without the vec0 extension writes blob rows (advancing the sidecar)
  and a vec-aware process later uses the same sidecar
- **THEN** the count mismatch is detected and the vec0 rows are rebuilt from blobs
- **AND** subsequent KNN results reflect the non-vec-aware writer's changes

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

### Requirement: Managed Lexical Repair Converges Under Live Traffic

While managed retrieval is unavailable, the system SHALL allow a detached full
lexical repair to publish under ordinary writer and watcher traffic without
weakening authoritative source, projection, policy, or semantic-identity
validation.

#### Scenario: Concurrent live write is rebased before publication

- **WHEN** a watcher generation changes or deletes Markdown paths while a
  detached full repair is building
- **AND** the complete bounded delta from the repair checkpoints to the current
  live checkpoints is retained
- **THEN** the system applies that delta to the completed replacement under the
  publication barrier
- **AND** publishes only after the replacement proves the current checkpoints
- **AND** managed retrieval can become ready without a process restart

#### Scenario: Large batch landing during publication wait catches up off-barrier

- **WHEN** a foreground watcher batch holds the publication barrier while a
  completed detached repair waits
- **AND** the retained final suffix is complete but exceeds the barrier replay cap
- **THEN** the system preserves the completed replacement and releases the barrier
- **AND** replays that suffix off-barrier without the foreground cap
- **AND** repeats the independent source proof before retrying publication
- **AND** limits catch-up retries so sustained live churn cannot monopolize repair

#### Scenario: Published handoff does not repeat a current full scan

- **WHEN** a successfully published and promoted repair leaves one generation
  request pending at its bounded idle handoff
- **AND** the next repair flight proves the persisted catalogue already covers
  the current live projection
- **THEN** the system acknowledges that handoff without another full-vault scan
- **AND** a stale proof or a repair request arriving during the proof still
  follows the normal full-repair path

#### Scenario: Safety-net reconcile proof survives restart

- **WHEN** the periodic safety-net walk discovers a filesystem event the live
  watcher missed
- **AND** it holds complete before and after recall maps under one unchanged
  policy identity
- **THEN** the system retains their exact changed/deleted set as a bridgeable
  recall delta carrying explicit reconcile provenance, never as a trusted
  watcher transition
- **AND** the lexical replay persists the resulting current checkpoint only
  after an independent off-barrier source proof matches that exact checkpoint
- **AND** a fresh process admits the current catalogue without a full rebuild

#### Scenario: Mixed reconcile walk cannot bless a stale catalogue

- **WHEN** the off-lock safety-net walk mixes pre- and post-change
  observations because a source path changed after the walk observed it
- **AND** the lexical replay applies the reconcile delta's changed/deleted set
- **THEN** the independent source proof fails to match the reconcile-derived
  checkpoint
- **AND** the system refuses to persist that scope's checkpoint and preserves
  the conservative repair path

#### Scenario: Source proof never holds the publication barrier

- **WHEN** a watcher batch must prove a reconcile-tainted checkpoint against
  the complete current source
- **THEN** the O(vault) proof walk executes before the publication barrier is
  acquired, with no locks held
- **AND** validation under the barrier is an O(1) exact-checkpoint comparison
- **AND** request and readiness paths refuse a reconcile-tainted delta without
  ever walking the vault

#### Scenario: Invalidated source proof refuses only the affected scope and converges

- **WHEN** an observed event, reconcile, or policy change lands between the
  off-barrier source proof and the publication barrier
- **THEN** the superseded proof fails closed and only the affected scope's
  checkpoint is refused
- **AND** sibling scopes with exact observed witnesses still persist
- **AND** the batch's rows still apply, and a later batch covering the current
  delta re-proves and persists the checkpoint without a full rebuild
- **AND** proof outcomes are counted in stable, content-free telemetry

#### Scenario: SQLite token-only churn does not veto a current replacement

- **WHEN** the live SQLite main, WAL, or SHM token changes during a detached
  repair
- **AND** source/projection checkpoints, policy, and semantic identity still
  match the replacement proof
- **THEN** token-only churn SHALL NOT decline publication

#### Scenario: Unprovable catch-up fails closed

- **WHEN** the required delta is incomplete
- **OR** the final suffix remains oversized after the bounded catch-up retries
- **OR** source, policy, projection identity, or semantic identity cannot be
  proven current
- **THEN** the system preserves the live catalogue
- **AND** leaves the repair request pending for a later bounded flight

#### Scenario: Repair telemetry preserves vault privacy

- **WHEN** a detached repair advances, publishes, or declines
- **THEN** telemetry reports a bounded phase, duration, and stable result reason
- **AND** contains no vault path, note name, or note content

