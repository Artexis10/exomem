## MODIFIED Requirements

### Requirement: Event-Maintained Inbound-Link Index

The system SHALL maintain the inbound wikilink index incrementally: when a specific set of markdown
files changes, the system SHALL update only the affected files' entries in the index (removing their
prior contributions and re-reading only those files) rather than re-scanning the entire vault. The
resulting index's content (which inbound links exist for a given target) MUST be identical to what a
full rebuild of the index would produce for the same vault state. When the incremental registry is
not live, explicit inbound-link operations and mutation safety checks MUST fall back to the existing
full-vault rebuild. An activated server read that uses inbound links only to enrich its result, such
as a context-pack neighbourhood, MUST NOT build the index on the request thread. It SHALL use the
links that the published index proves, mark its inbound part incomplete, and leave recovery to the
existing watcher owner.

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

- **WHEN** the incremental inbound-link registry is not live and an explicit inbound-link operation
  or a mutation safety check needs inbound links
- **THEN** the inbound-link index is computed by a full-vault rebuild, exactly as before this
  capability existed

#### Scenario: A context-pack neighbourhood does not build the index

- **WHEN** an activated server request builds a context-pack neighbourhood and the published inbound
  index cannot prove the page's inbound links
- **THEN** the request returns its outbound neighbours and marks the inbound neighbours incomplete
- **AND** the request thread reads no page to rebuild the index
- **AND** the existing watcher owner recovers the index for a later request

#### Scenario: A self-written link is visible without a rebuild

- **WHEN** a service write adds a wikilink to a page and the inbound index was published before the
  write
- **THEN** the write path patches and republishes the index in proportion to the change
- **AND** the next context-pack neighbourhood for the target includes the new inbound link without a
  full-vault rebuild

## ADDED Requirements

### Requirement: Read requests use published derived generations

An activated server read request SHALL NOT walk the vault, read every page, or rebuild or catch up a
whole derived structure on the request thread. Each derived structure that a read consults SHALL be
advanced by its publisher, which is the write path, the file watcher, the derived drain or an
existing background owner. The request SHALL read the structure's last published generation. A
caller's own committed service write SHALL be visible to its next read, through an update on the
write path in proportion to the change or through the pending-visibility overlay. A change from
another source SHALL become visible within the publisher's bound, and periodic reconciliation SHALL
bound missed events. A structure without a published generation MUST NOT read as empty: the read
SHALL keep its synchronous path or report incomplete coverage. Release, revocation and
access-policy decisions SHALL remain per request. A response flag that reports a structure as stale,
lagging, warming or incomplete SHALL be computed relative to the caller's view: for a restricted
caller, it SHALL report the lag only when a changed path that the caller may see is missing, so
that writes to material the caller may not see do not change the response. To keep the
per-request cost bounded, a flag SHALL report the lag without deciding each path when more changed
paths than a fixed bound separate the published generation from the current state, or when the
change history between them cannot be bridged; a burst of writes to withheld material can then
change a restricted caller's response until the structure is published. Explicit offline
callers and deployments with event-maintained indexes disabled MAY keep their synchronous paths.

#### Scenario: A caller's own write is visible on its next read

- **WHEN** a caller commits a page through the service and recalls it on its next request
- **THEN** the recall can return the page through the pending-visibility overlay or the updated
  structure
- **AND** the request does not catch up the embedding matrix or rebuild another whole structure

#### Scenario: A lagging structure is served while its publisher catches up

- **WHEN** a derived structure is behind its stored generation and a read request needs it
- **THEN** the request reads the last published generation and starts or joins the publisher's
  single-flight catch-up in the background
- **AND** text or rows for paths changed since that generation are not served from it

#### Scenario: An external edit appears within the publisher's bound

- **WHEN** a file changes outside the service and the watcher observes the change
- **THEN** each affected structure publishes a generation that includes the change within its
  publisher's bound
- **AND** a missed event is corrected by the next periodic reconciliation

#### Scenario: A policy change is not served from an older generation

- **WHEN** the access policy, grants or audience of a request differ from those a published
  generation was built for
- **THEN** the request does not use that generation
- **AND** it returns warming or incomplete coverage, or takes its synchronous path

#### Scenario: An unpublished structure is never read as empty

- **WHEN** a read request needs a derived structure that has no published generation
- **THEN** the request keeps the structure's synchronous path or reports incomplete coverage
- **AND** it never treats the structure as empty

#### Scenario: Coverage flags follow the caller's view

- **WHEN** a published generation is behind only by changes to paths that a restricted caller may
  not see
- **THEN** the caller's response reports that structure's coverage as complete
- **AND** the response is the same as it would be with no such change
- **AND** a lag that includes a path the caller may see is reported as incomplete or stale
- **AND** a lag longer than the decision bound, or one whose change history cannot be bridged,
  is reported as incomplete or stale without a decision per path

#### Scenario: Withheld publication between activation selections

- **WHEN** a catalogue publication changes only withheld pages between activation's initial lexical selection and compilation
- **THEN** compilation may use its captured catalogue checkpoint after proving both selections equivalent under current caller authority
- **AND** compilation keeps its checkpoint and verdicts separate from earlier selections
- **AND** the restricted response matches the response without that publication
- **AND** a visible change, changed authority or invalid selected source retains the existing incomplete or unavailable outcome

#### Scenario: A withheld source leaves a retained consumer publication

- **WHEN** a bounded, bridged consumer delta deletes a source that an existing exact catalogue verdict withheld under the caller's fresh authority
- **THEN** the retained Graph or direct inbound result may remain complete only when the verdict's full source hash matches that consumer's earlier source
- **AND** Graph reads the expected hash from the same retained SQL publication and requires its recall checkpoint to match the catalogue view's vault checkpoint
- **AND** every catalogue verdict uses its own scope checkpoint and exact SQL generation
- **AND** inbound source records authorize proof only at their proven complete checkpoint, with partial patches invalidating that association
- **AND** the request pins proved deleted paths before constructing the consuming reader and preserves an outer request's exclusions
- **AND** retained text, edges, inbound contexts and basename evidence from those paths remain withheld
- **AND** changed bytes, revoked authority, missing proof, unavailable scope custody and existing unbridgeable or over-bound history keep the incomplete outcome

#### Scenario: Partial graph repair preserves a complete published view

- **WHEN** a partial graph repair changes rows after a complete publication
- **THEN** a restricted reader may use that publication only with coherent rows and matching immutable resolver topology
- **AND** each request keeps an independent SQL transaction and deadline
- **AND** admission and finalization verify durable lineage, current caller authority and selected sources
- **AND** a foreign write without a complete local bridge retains the existing incomplete or unavailable outcome
- **AND** repeated replacements preserve the first predecessor, while create-delete cycles retire obsolete absence markers
- **AND** a complete publication updates metadata and retires predecessors atomically

#### Scenario: Foreign integrity repair invalidates an open correction read

- **WHEN** another process purges graph rows before admission or during hydration
- **THEN** correction discovery reports unavailable and returns no tentative corrections
- **AND** admission and finalization check the captured graph incarnation through a fresh connection
- **AND** the check also applies without a retained process publication
- **AND** a subsequent complete recovery cannot validate the purged incarnation
- **AND** ordinary incremental publication keeps its incarnation and may preserve withheld-only correction context

#### Scenario: A foreign resolver input changes before watcher delivery

- **WHEN** a canonical writer changes a resolver input outside the KB before the serving worker observes it
- **THEN** a later withheld KB write cannot establish continuity from an epoch that omitted that earlier mutation
- **AND** epoch coverage includes admitted collection manifests, case-insensitive Markdown extensions and membership changes before and after mutations
- **AND** renames cover both endpoints before source mutation through the existing lifecycle ordering
- **AND** an input outside the KB uses full recovery instead of entering the KB row queue
- **AND** a new baseline requires participating writers with complete epoch coverage and a complete post-upgrade publication
- **AND** the bridge claims no observation of arbitrary filesystem edits before watcher delivery

#### Scenario: A corpus callback proves one canonical epoch

- **WHEN** a complete callback observes the next paths-scoped canonical epoch
- **THEN** the bridge advances only when the consumer delta names every epoch endpoint with matching classification, target signatures and guarded bytes
- **AND** two short canonical holds verify the same predecessor and durable epoch around corpus publication
- **AND** a full checkpoint, generation gap, missing endpoint or source-proof-required history retains the unavailable outcome
- **AND** a duplicate callback cannot attach a newer local checkpoint to the same canonical epoch
- **AND** only a successful full-source publication may restore a lost association

#### Scenario: Concurrent correction readers keep independent deadlines

- **WHEN** restricted correction readers need the same retained publication concurrently
- **THEN** short proof boundaries may queue within each request's deadline without sharing query connections or progress handlers
- **AND** queries, hydration and release decisions hold neither the reader mutex nor canonical authority
- **AND** a reader that spends its budget in that queue reports budget exhaustion before fallback work
- **AND** an active canonical writer retains the existing nonblocking unavailable outcome

#### Scenario: Offline callers keep their synchronous paths

- **WHEN** an explicit offline caller or a deployment with event-maintained indexes disabled reads
- **THEN** the caller may walk, scan or rebuild as before this requirement existed
