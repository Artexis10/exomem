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
that writes to material the caller may not see do not change the response. Explicit offline
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

#### Scenario: Offline callers keep their synchronous paths

- **WHEN** an explicit offline caller or a deployment with event-maintained indexes disabled reads
- **THEN** the caller may walk, scan or rebuild as before this requirement existed
