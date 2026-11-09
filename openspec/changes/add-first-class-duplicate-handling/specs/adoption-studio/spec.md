## MODIFIED Requirements

### Requirement: Exact-action preview bound by plan identity

`adoption_studio(action="plan")` SHALL read and sha256-hash each selected file at plan time, compute the exact deterministic target path, title, and frontmatter for each item, record any skipped items with a code, and persist a `plan` carrying a `plan_id` derived from the selection hash and the complete planned actions: each item's destination, classification, provenance fields, rendered link projection, and original hash. Destinations SHALL be allocated before links resolve and SHALL NOT change afterwards. A text item that is not valid UTF-8 SHALL plan as an artifact. A classified item's target SHALL be in its kind's Source directory (`Sources/<Kind>/<Domain>/`); an unclassified item's target SHALL be in `Sources/Imported/`. For a text item, `plan` SHALL read its wikilinks, embeds, and local Markdown links and images with the shared parsers, and SHALL resolve each target against the planned items and earlier imports by exact collection-relative path, then note-relative path, then unique basename, then unique front-matter alias, leaving an ambiguous or missing target unresolved, and SHALL record the resolved targets, the unresolved targets, the aliases it carries, its tags, and any tag the vault's tag normalization rejects. A text item's tags SHALL be its front-matter tags followed by the inline `#tags` of its body that the shared Markdown parser reads outside code, comments and escapes; a hash inside code SHALL NOT become a tag. `plan` SHALL check each item against earlier imports by `import_collection` and `collection_path` (falling back to `imported_from` for an import that lacks them, and reading each `also_imported_from` entry of a Source as an earlier import of its own), SHALL record `existing_ref` for an item whose original an earlier import holds with the same bytes, and SHALL record the latest earlier Source as `previous_import` for an item whose bytes changed. `plan` SHALL also check each item's bytes against the vault's stored originals and Source payloads of the same lane through the duplicate-handling index, and against every earlier item of the same plan. An item whose bytes the vault stores under another path or collection SHALL record `existing_ref` and the import mapping that `apply` would add. An item whose bytes an earlier item of the plan holds SHALL record `duplicate_of` naming that item, SHALL plan no Source or artifact of its own, and SHALL resolve links to it as links to that earlier item. The plan SHALL report the total bytes that `apply` will write, which exclude every such item. A warning the plan raises, such as an earlier import of the collection from another root, SHALL appear in the plan's `warnings` and in the top-level `warnings` of every reply for that run, where the mutation terminal reads the warning texts and the warning count. When the remaining request budget falls below its margin, `plan` SHALL stop, return `progress` and a `continuation`, and continue on a call that passes that cursor. `plan` SHALL write nothing outside the run object (targets are previewed, not created). The preview a caller sees SHALL be exactly what a subsequent `apply` commits, or `apply` SHALL refuse.

#### Scenario: Plan previews exact targets without writing

- **WHEN** `plan` runs over a non-empty selection
- **THEN** each item records its `original_sha256`, exact `target_path`, `title`, and `frontmatter`, and a stable `plan_id` for identical inputs
- **AND** a byte snapshot of the vault before and after `plan` is identical outside the run object

#### Scenario: Replacement cannot redirect an original read

- **WHEN** a selected original or its parent is replaced with a link during planning or apply
- **THEN** retained no-follow custody prevents the read from following that link
- **AND** a changed binding produces a coded refusal before publication instead of importing bytes from the replacement

#### Scenario: Plan resolves links without guessing

- **WHEN** a planned note links to another planned note by path, embeds a planned image by basename, links to a third note through its front-matter alias, and links to a basename that two planned notes share
- **THEN** the first three targets are recorded as resolved to those items' frozen target paths
- **AND** the shared basename is recorded as unresolved

#### Scenario: Plan reads inline tags outside code

- **WHEN** a planned note carries a front-matter tag, an inline `#tag` in its body, and a hash inside an inline code span and inside a fenced code block
- **THEN** the item's tags are the front-matter tag followed by the inline tag
- **AND** neither hash inside code is a tag

#### Scenario: Plan recognizes an earlier import

- **WHEN** a collection that an earlier run imported is staged again under a new location with the same `collection` label and one note changed
- **THEN** every unchanged item records the `existing_ref` of its earlier Source
- **AND** the changed note plans as a new item that names its earlier Source as `previous_import`

#### Scenario: Apply refuses a stale plan

- **WHEN** `apply` is called with a `plan_id` that does not equal the stored plan's id, or the current selection hash no longer matches the plan's selection hash
- **THEN** it is refused with `PLAN_STALE` and writes nothing

#### Scenario: A tree holds two identical photos

- **WHEN** `plan` runs over a tree whose two photos have identical bytes in different folders
- **THEN** the first photo plans as a new Source with its artifact
- **AND** the second records `duplicate_of` naming the first and plans no Source or artifact
- **AND** the reported bytes to write count the photo once

#### Scenario: A tree repeats bytes the vault already stores

- **WHEN** `plan` runs over a file whose bytes a Source from another import already stores under a different collection path
- **THEN** the item records that Source's `existing_ref` and the mapping `apply` would add
- **AND** the plan writes nothing outside the run object

### Requirement: Governed apply with write-time re-validation

`adoption_studio(action="apply")` SHALL process the planned items that have no outcome yet, in plan order, in chunks bounded by item count and bytes. For each item it SHALL re-read the original bytes and re-verify the sha256 against the value captured at `plan` immediately before writing. Before each write it SHALL repeat the collection identity check and the duplicate lookup under its mutation guard. Each chunk SHALL commit its text pages, streamed artifacts, outcomes, Sources index, top index and log update in one `batch_atomic_write`, and, outside crash recovery, an item planned as `duplicate_of` that falls back to storing its own copy SHALL commit in a second `batch_atomic_write` of that chunk. Index and log updates SHALL use the actual destinations. Artifact writes SHALL use the Source artifact seam shared with capture and SHALL keep the original bytes. An extraction request SHALL follow each artifact commit. An item whose bytes changed SHALL be excluded with per-item outcome `SOURCE_CHANGED`; a missing item SHALL be excluded with `NOT_FOUND`; an item whose frozen destination another writer now occupies SHALL fail with `TARGET_OCCUPIED` and SHALL NOT move; an item with `existing_ref`, or one that the identity check finds already committed, SHALL record `already-applied` with that ref and write no Source and no artifact. Where such an item's own `import_collection` and `collection_path` are not yet recorded on that Source, `apply` SHALL add one import mapping to that Source's frontmatter under the mutation guard, guarded by the page's content hash and leaving its captured text and original bytes unchanged, and SHALL mark the outcome `mapped: true`. Where they are recorded, it SHALL write nothing. An item planned as `duplicate_of` another item SHALL do the same against that item's Source once that item has committed, and SHALL record `already-applied` with that Source's ref. For an item planned with an import mapping or as a `duplicate_of`, `apply` SHALL confirm the Source that the plan named, its `existing_ref` or the Source of the item it repeats, by reading that Source's stored bytes again, without the duplicate index. When that Source is missing or no longer holds the bytes, and the duplicate lookup finds no other Source that holds them and that the caller can write, `apply` SHALL store the item's own bytes as an ordinary item and mark its outcome `stored_own_copy`, and SHALL NOT fail it as `PLAN_STALE`. A re-run item of an unchanged path, which the plan found by its earlier import and planned with no mapping, SHALL still fail `PLAN_STALE` when its earlier Source page or artifact is missing or the page records a different hash for that path, so that a re-plan sees the current vault. Applied Sources SHALL carry `imported_from`, `import_collection`, `collection_path`, and `original_sha256` provenance for the first path that stored them, and one `also_imported_from` entry (`imported_from`, `import_collection`, `collection_path`) for each further path, so that every source path is recorded. A text Source SHALL carry the original text verbatim in its fenced capture, so that the captured text encodes to bytes whose sha256 equals `original_sha256`, and its resolved links in a generated `## Links` section outside that fence. When the remaining request budget falls below its margin, `apply` SHALL stop between commits and return `progress` (`applied`, `remaining`, `total`) and a `continuation`; an `apply` passing that cursor with the same `plan_id` SHALL continue, and an `apply` repeating an earlier cursor SHALL return that call's result again. The phase SHALL become `applied` only when every planned item has an outcome. The result SHALL include `verified_unchanged` and `verified_total` counts from a post-apply re-hash of the originals handled by that call. The result SHALL include `skipped_duplicates`: the count of the run's items that it did not store because a Source already held their bytes, whether an earlier item of the plan or an earlier run stored them. The default reply of `apply` SHALL carry it as `skipped_duplicates`, and `response_detail="full"` SHALL also hold it under `diagnostics.apply_result`. A `plan`/`apply` invalidated by inventory drift SHALL surface `ADOPTION_SOURCE_CHANGED` so the client can re-scan while preserving the still-valid selection.

#### Scenario: Apply copies with provenance and leaves originals byte-identical

- **WHEN** `apply` commits a validated plan
- **THEN** each imported Source carries `imported_from`, `import_collection`, `collection_path`, and `original_sha256`, the Sources index and log are updated with each call, and every original file is byte-identical before and after
- **AND** the result reports `verified_unchanged` equal to `verified_total`

#### Scenario: Attachments and links land together

- **WHEN** `apply` commits a plan holding a note that embeds a planned PDF and links to another planned note
- **THEN** the PDF is a Source whose artifact bytes equal the original
- **AND** the note's Source holds the original text verbatim in its fenced capture and a `## Links` section with links to the PDF's Source and the other note's Source
- **AND** an extraction request exists for the PDF

#### Scenario: A large plan finishes over several calls

- **WHEN** `apply` reaches its request budget with items remaining
- **THEN** it returns `progress` with a nonzero `remaining` and a `continuation`
- **AND** calls that pass each returned cursor commit only the remaining items, each item once, until `remaining` is zero and the phase is `applied`
- **AND** repeating an earlier cursor returns that call's result again and writes nothing

#### Scenario: A source changed after plan is excluded, not fatal

- **WHEN** one selected original is modified between `plan` and `apply` and others are unchanged
- **THEN** the changed item records outcome `SOURCE_CHANGED`, the unchanged items are applied, and the run phase becomes `partial`

#### Scenario: Identical photos import as one original with both paths

- **WHEN** `apply` commits a plan whose two photos have identical bytes
- **THEN** one Source and one artifact exist, byte-identical to the photo
- **AND** the Source records the second photo's path in `also_imported_from`
- **AND** the second item's outcome is `already-applied` with `mapped: true` and the first item's ref
- **AND** the `apply` reply reports `skipped_duplicates` of 1

#### Scenario: Apply needs no duplicate index for a Source the plan named

- **WHEN** `apply` commits a plan with two identical photos while the duplicate index is not built
- **THEN** the second item's outcome is `already-applied` with `mapped: true` and the first item's ref, and the run reaches `applied`
- **AND** when the first photo changed after the plan, the second item is stored as a Source of its own with `stored_own_copy` in its outcome, not failed as `PLAN_STALE`

#### Scenario: A plan warning reaches the reply's warning count

- **WHEN** a collection that an earlier run imported from one root is planned from another root
- **THEN** the plan reply and the apply reply each carry that warning in `warnings`
- **AND** `diagnostics.operational.warnings_count` counts it under `response_detail="full"`

#### Scenario: A second run writes nothing

- **WHEN** the same tree is planned and applied again unchanged
- **THEN** every item reports `already-applied` and no file in the vault changes

#### Scenario: The staging root moves

- **WHEN** the same collection is staged under a new root and imported again with the same collection label
- **THEN** every item matches by collection path and the run writes nothing

#### Scenario: A file is renamed inside the collection

- **WHEN** one file of an imported collection is renamed and the collection is imported again
- **THEN** the renamed item reports `already-applied` and adds one import mapping for its new collection path
- **AND** the next run writes nothing
