## MODIFIED Requirements

### Requirement: Append-Only Tree Relocation

A move that stays within one append-only tree SHALL be permitted. It SHALL carry the body and every field verbatim, and the page's lineage entry SHALL be its only byte change. A move from `Sources/` into `Evidence/` SHALL be permitted as a promotion when the caller supplies a promotion reason, and the reason MUST be recorded in the activity log. Every other boundary crossing MUST be refused, including any move out of `Evidence/` and any move into an append-only tree from a non-append-only location. Promotion MUST NOT change file content; its only byte change SHALL be the lineage entry.

A move that updates wikilinks SHALL repoint each link inside a page in `Sources/` or `Evidence/` through the governed pointer write when the moved page carries an `exomem_id`. When the moved page carries no `exomem_id`, a move that would rewrite such a link SHALL be refused with `APPEND_ONLY`, as before. A move SHALL also be refused with `APPEND_ONLY` when its change to such a page is not a pointer change. A page withheld from a restricted mover stays unchanged instead, and the move commits. A generated `index.md` in those trees is not captured content, and the move SHALL rewrite it. A path in an `Evidence/` page's `ingested_into` field is a pointer field, and the move SHALL repoint it through the same governed pointer write. The body outside the repointed spans and every other field of that page SHALL keep their bytes.

Every governed move SHALL follow this rule, in file mode and in store mode. A reclassification is a governed move: it SHALL repoint its append-only referrers in the same way, and its revert SHALL restore them from their pointer-write records. Lineage SHALL stay the safety net for every reference that a move does not write.

#### Scenario: Source becomes case-relevant

- **WHEN** a caller moves a page from `Sources/` to `Evidence/<scope>/` with a promotion reason
- **THEN** the file is relocated with its body and fields unchanged and one lineage entry added
- **AND** the activity log records the promotion together with the supplied reason

#### Scenario: Promotion without a stated reason

- **WHEN** a caller moves a page from `Sources/` to `Evidence/<scope>/` without a promotion reason
- **THEN** the move is refused
- **AND** the refusal names the missing reason rather than the append-only rule

#### Scenario: Evidence is never demoted

- **WHEN** a caller moves a page out of `Evidence/` to any destination
- **THEN** the move is refused regardless of any reason supplied
- **AND** the refusal states that a case scope must remain complete

#### Scenario: Outside content still lands through the capture writers

- **WHEN** a caller moves a page from a non-append-only location into `Sources/` or `Evidence/`
- **THEN** the move is refused
- **AND** the refusal directs the caller to `add` or `preserve`

#### Scenario: Evidence names the moved note as ingested

- **WHEN** a caller previews and then moves a note with `update_wikilinks=true` and the preview digest, and an `Evidence/` page names that note only in its `ingested_into` field
- **THEN** the move commits and the `ingested_into` entry names the new path
- **AND** the page's body and other fields keep their bytes

#### Scenario: A link in Evidence content is repointed

- **WHEN** an `Evidence/` page links the moved note from its body, the note carries an `exomem_id`, and the caller passes the preview digest
- **THEN** the move commits and the link names the new path
- **AND** every other byte of the `Evidence/` page is unchanged, and the pointer-write record holds the old link

#### Scenario: A store-mode move repoints a link in Evidence content

- **WHEN** a caller moves a note that carries an `exomem_id` in a store-mode vault with the preview digest, and an `Evidence/` page links the note from its body
- **THEN** the move commits and the link names the note's new location and still resolves to the note
- **AND** the page's bytes differ from before only in that link's target text
- **AND** the pointer-write record holds the repoint with its reason

#### Scenario: A link in Evidence content still blocks the move

- **WHEN** an `Evidence/` page links the moved note from its body and the note carries no `exomem_id`
- **THEN** the move is refused with `APPEND_ONLY`
- **AND** no page is changed

## ADDED Requirements

### Requirement: A moved page records its prior paths

Each move of a Markdown page that has a frontmatter block SHALL append one
entry naming the old path to the page's `moved_from` list, oldest first, in the
same commit as the move. A reclassification entry SHALL also carry the prior
classification keys. A legacy `reclassified_from` value SHALL read as lineage
entries older than every `moved_from` entry, and no writer SHALL rewrite it to
rename the key. A page without a frontmatter block SHALL move without lineage.

An undo SHALL remove the lineage entry that its forward move appended and
SHALL append none. A reclassification revert and a curation compensation are
undos. A plain move back to an old path is a move and appends an entry.

#### Scenario: A note moves twice

- **WHEN** a note moves from path A to path B and then to path C
- **THEN** the note at C lists A and then B in `moved_from`
- **AND** links to A and to B resolve to the note at C

#### Scenario: A reclassified Source moves again

- **WHEN** a Source that records a legacy `reclassified_from` entry moves
- **THEN** its `reclassified_from` value keeps its bytes and the new entry goes
  to `moved_from`

#### Scenario: A revert removes its own entry

- **WHEN** a reclassification is reverted
- **THEN** the Source no longer lists the path it returned from, and no entry
  names the path it left

### Requirement: Links resolve through a page's prior paths

A prior path SHALL come from one of two durable lineage sources:

- the page's own lineage: its `moved_from` entries and any legacy
  `reclassified_from` entries;
- a row of the relayout record that `adopt-root-vault-layout` keeps in the
  vault. Each row names an `exomem_id` or none, an old path and a new path.

A relayout row with an `exomem_id` SHALL claim its old path for the live page
that holds that `exomem_id`. A row without one SHALL claim its old path for the
live page whose current path or lineage holds the row's new path. A bulk
relayout writes no `moved_from`. A prior path from either source SHALL follow
the same resolution, visibility, reserved-path and squatter-guard rules. A row
that a relayout revert reverses SHALL claim nothing.

Link resolution SHALL try, in order: the exact path, the wrapper-prefixed path,
a prior path, a retired collection path, the stem, the title, and a prior stem.
A prior path or prior stem SHALL resolve only when exactly one live page that
the caller may see claims it. The visibility filter SHALL run before claimants
are counted, so a prior path that only a withheld page claims SHALL answer as
missing. A lineage entry equal to its page's current path SHALL claim nothing.
A prior stem SHALL never win over a live name.

Read commands, link rendering inside written content, the graph, the audit and
inbound-link checks SHALL use this resolution. The target path argument of a
write command SHALL stay literal and SHALL NOT follow lineage. Inbound-link
checks for a delete or a move SHALL read the target's lineage from its own
bytes. The prior-path lookup SHALL read an indexed derived table. The identity
writers SHALL refill that table from page bytes and the relayout record, and a
rebuild from those two sources alone SHALL give the same rows. No read or write
SHALL scan the vault for lineage. The audit SHALL report each prior path that
two live pages claim as `prior_path_conflict`.

#### Scenario: An old link resolves after a move

- **WHEN** a Source keeps a link to a note's old path after the note moves
- **THEN** reading the Source's links resolves that link to the note's new path
- **AND** the graph holds the edge to the note at its new path

#### Scenario: An old link resolves after a relayout

- **WHEN** a relayout moves a note without writing `moved_from`, its record
  holds the note's `exomem_id`, old path and new path, and the note later moves
  again
- **THEN** a link to the relayout's old path resolves to the note at its
  current path
- **AND** a rebuild of the prior-path index gives the same resolution

#### Scenario: A withheld page claims the prior path

- **WHEN** a restricted caller follows a link whose prior path only a page
  withheld from that caller claims
- **THEN** the link answers as missing, exactly as a link to a path no page
  claims

#### Scenario: Two pages claim one prior path

- **WHEN** two live pages list the same prior path
- **THEN** a link to that path stays unresolved
- **AND** the audit reports both claimants as `prior_path_conflict`

#### Scenario: An edit names an old path

- **WHEN** a caller edits a page by a path that only a lineage entry names
- **THEN** the edit acts on that literal path and does not follow the lineage
  to the moved page

### Requirement: A prior path stays reserved while a live page claims it

`unique_path` and every create-only check SHALL treat a prior path that a live
page claims as taken. A move of a page to a path in its own lineage SHALL be
permitted. When the claimant is withheld from the caller, the refusal SHALL be
the non-disclosing `WRITE_REFUSED`.

A file without an `exomem_id` at a prior path that a live page claims SHALL
NOT take that address: links to the path SHALL keep resolving to the lineage
claimant, and the audit SHALL report the file as `reserved_path_occupied`.

#### Scenario: A create lands on a reserved path

- **WHEN** a caller creates a page at a path that a moved page lists in
  `moved_from`
- **THEN** the create refuses and names the claimant to a caller that may see it

#### Scenario: A create lands on a relayout's old path

- **WHEN** a caller creates a page at an old path that a relayout record row
  claims for a live page
- **THEN** the create refuses exactly as for a path in a page's `moved_from`

#### Scenario: A page moves back

- **WHEN** a page moves back to a path in its own lineage
- **THEN** the move commits

#### Scenario: An editor creates an empty file at the old path

- **WHEN** an editor creates a file without an `exomem_id` at a moved page's
  old path
- **THEN** links to that path still resolve to the moved page
- **AND** the audit reports the file as `reserved_path_occupied`

#### Scenario: A withheld page holds the reserved path

- **WHEN** a restricted caller creates a page at a prior path that only a
  withheld page claims
- **THEN** the create refuses with `WRITE_REFUSED`, byte for byte as for any
  unavailable target

### Requirement: Append-only pages change only in pointer spans

After capture, every write to a page in `Sources/` or `Evidence/` SHALL go
through the governed pointer write. The capture writers that finish a page's
processing record or assign its `exomem_id` SHALL keep their own rules. Each
caller of the pointer write SHALL declare the spans it intends to change. The
write SHALL admit a span only when it is one of these:

- a wikilink target whose old and new forms resolve to the same `exomem_id`;
- the value of a frontmatter field that the `page-fields` registry declares
  with `role: pointer`;
- through a move, the page's own folder and the lineage entry that the move
  appends or its undo removes.

The write SHALL parse the page and compare its before and after spans. Every
byte outside an admitted, declared span MUST be identical, and any other
difference SHALL refuse with `APPEND_ONLY`. Original media, body text and
provenance MUST NOT change.

A call SHALL approve the pointer writes to the page it names: a direct move,
promotion, reclassification or in-place pointer correction of that page SHALL
need no digest. A pointer write to a page that the call did not name, such as
a referrer repoint, SHALL be approved either by a curation plan approval or by
a commit bound to the digest of the preview that listed the change. A commit
without that digest, or with a digest whose bindings no longer match, SHALL
refuse with `POINTER_PREVIEW_REQUIRED`, change nothing, and name the preview
call.

Each pointer write SHALL create one create-only, owner-only record. The record
SHALL name the page, the write's reason, its before and after content hashes,
and each changed span's location, old bytes and new bytes. A revert SHALL apply
only while the page holds the recorded after hash, and its result SHALL hash to
the recorded before hash.

#### Scenario: A move repoints a Source's link

- **WHEN** a pointer write repoints a Source's link to a moved note
- **THEN** only the declared link span differs
- **AND** the Source's body outside that span, its provenance fields and its
  original media keep their hashes

#### Scenario: A crafted repoint names another page

- **WHEN** a declared link span's new target resolves to a page with a
  different `exomem_id`
- **THEN** the write refuses with `APPEND_ONLY` and nothing changes

#### Scenario: A change outside the declared spans

- **WHEN** a pointer write's result differs from the page in a field the
  registry does not declare as a pointer, or in body text outside a link target
- **THEN** the write refuses with `APPEND_ONLY` and nothing changes

#### Scenario: A pointer write is reverted

- **WHEN** the owner reverts a pointer write while the page holds the recorded
  after hash
- **THEN** the page's bytes equal its bytes before the write

#### Scenario: A revert after later work

- **WHEN** the page no longer holds the recorded after hash
- **THEN** the revert refuses and the page is unchanged

### Requirement: A pointer field on an append-only page can be corrected in place

`edit_memory` with a `patch_frontmatter` operation SHALL correct one field of a
page in `Sources/` or `Evidence/` when the `page-fields` registry declares that
field with `role: pointer`. The correction SHALL run as a governed pointer
write and SHALL NOT move the page. It SHALL change only that field's value
span. It SHALL NOT set `updated` or any other field, and SHALL keep every other
byte, line endings included. The new value SHALL pass the same field
validation as on any other page. The call that names the page SHALL be the
correction's approval, and the correction SHALL create a pointer-write record.
A `patch_frontmatter` operation on any other field of such a page SHALL keep
refusing with `APPEND_ONLY`. A preview of the correction SHALL return the old
and new value.

#### Scenario: A Source's project is corrected

- **WHEN** a caller applies a `patch_frontmatter` correction of a Source's
  `project` field
- **THEN** only the `project` value span differs
- **AND** the Source's `updated` field, body and original media keep their
  bytes, and the pointer-write record holds the old value

#### Scenario: A field that is not a pointer stays immutable

- **WHEN** a caller applies a `patch_frontmatter` operation to a Source's
  `title` field
- **THEN** the write refuses with `APPEND_ONLY` and nothing changes

#### Scenario: A correction is reverted

- **WHEN** the owner reverts a `project` correction while the Source holds the
  recorded after hash
- **THEN** the Source's bytes equal its bytes before the correction

### Requirement: A move previews one outcome per referrer

A move preview SHALL give each referrer exactly one outcome: `rewritten`,
`pointer-repointed (append-only page)`, `unchanged: resolves via prior path`,
or `refused` with its cause. A reference that Exomem does not write, such as a
path in a Records row, a planning item or an Evidence JSON file, SHALL be
`unchanged: resolves via prior path`. A restricted mover's preview SHALL list
only pages it may see, and its answer SHALL match a vault without the withheld
pages. The preview SHALL return a digest of its outcomes and bindings. A move
that repoints an append-only page SHALL commit only with that digest, under
the approval rule of the governed pointer write.

#### Scenario: The preview names every outcome

- **WHEN** a caller previews moving a note that a writable note, a Source and a
  Records row reference by path
- **THEN** the preview lists the writable note as `rewritten`, the Source as
  `pointer-repointed (append-only page)` and the Records row as
  `unchanged: resolves via prior path`

#### Scenario: A repoint without the preview digest

- **WHEN** a caller moves a note that a Source links by path without passing
  the preview digest
- **THEN** the move refuses with `POINTER_PREVIEW_REQUIRED` and nothing changes
