## MODIFIED Requirements

### Requirement: Reclassification preserves every reference to the source

The operation SHALL rewrite every inbound reference held in a mutable page so that no reference dangles, including references held in other pages' frontmatter provenance lists. A reference counts as rewritten only when its bytes change.

A reference held in an append-only page that resolves unchanged, such as a basename or title link, SHALL keep its bytes and SHALL NOT cause a refusal. The operation SHALL repoint each path link held in an append-only page through the governed pointer write, so that only the link's target span changes. It SHALL refuse with `APPEND_ONLY` when such a link must change and the source carries no `exomem_id`. A reclassification that repoints an append-only page SHALL commit only with the digest of its preview, under the approval rule of the governed pointer write.

The operation SHALL append the previous path, kind and domain to the source's `moved_from` list, and SHALL write it without changing the source's body bytes. A `reclassified_from` value, the legacy spelling, SHALL read as entries older than every `moved_from` entry. A legacy scalar value SHALL read as one entry with an unknown classification.

A reclassification SHALL be atomic: either the relocation, the companion move, the metadata correction, the history entry, every reference rewrite and every pointer repoint all apply, or none of them do.

#### Scenario: Inbound references follow the source

- **WHEN** a source cited by a compiled note's provenance list and linked by path from another mutable page is reclassified to a new location
- **THEN** the citing note's provenance entry names the new location
- **AND** the linking page's reference names the new location

#### Scenario: An append-only basename link does not block the move

- **WHEN** an Evidence page links a source by its basename and the source is reclassified
- **THEN** the move succeeds and the Evidence page is unchanged
- **AND** the link still resolves to the source

#### Scenario: An append-only path link is repointed

- **WHEN** an Evidence page links a source that carries an `exomem_id` by its full path, and the source is reclassified to a new location with its preview digest
- **THEN** the move succeeds and the Evidence page's link names the new location
- **AND** every other byte of the Evidence page is unchanged, and the pointer-write record holds the old link

#### Scenario: A repoint without the preview digest

- **WHEN** an Evidence page links a source by its full path and a caller reclassifies the source without the digest of its preview
- **THEN** the reclassification refuses with `POINTER_PREVIEW_REQUIRED` and nothing changes

#### Scenario: The previous path stays discoverable

- **WHEN** a source is reclassified twice
- **THEN** the source records both previous locations with their kinds and domains, oldest first
- **AND** a link to either previous location resolves to the source

#### Scenario: A failure part-way leaves nothing half-applied

- **WHEN** a reclassification fails while applying its changes
- **THEN** the source remains at its original location with its original classification and history
- **AND** no inbound reference has been rewritten

### Requirement: Reclassification reports what it would do before doing it

The operation SHALL offer a read-only mode that reports the corrected classification, the location the source would move to, the mutable references that would be rewritten, the append-only references that it would repoint, the append-only references that stay unchanged grouped by link form, the digest that a commit needs when it repoints an append-only reference, any refusal with its cause, and the evidence supporting each proposed value, without writing anything. The report SHALL name only pages the caller can see.

The read-only mode SHALL accept a caller-supplied kind and domain and preview that correction, so a caller that has read the source and decided can show the destination and affected references before anything is written. Supplied values SHALL be resolved through the same rules the correction applies, so a value the correction would refuse is refused during the preview rather than after approval.

A served file-mode preview for an unrestricted owner with a live observer ready for background recovery SHALL use complete inbound evidence and check the prose candidates available to the shared rewrite planner. The prose query SHALL prove current published rows and read those rows on one deadline-bound catalogue transaction. The observer settles pending changes or restores evidence in the background. A managed preview that cannot establish completeness or finish within its request deadline SHALL return `RECLASSIFICATION_PREVIEW_WARMING` with a retry delay. A search backend that cannot provide this proof SHALL return `RECLASSIFICATION_PREVIEW_UNAVAILABLE` without a retry delay. Both refusals SHALL omit reference counts and the preview digest. Evidence of zero references counts as complete.

When complete candidate evidence exceeds the interactive work or response bound, the managed preview SHALL start or rejoin a read-only job and return its handle. The completed receipt SHALL expose the full report through bounded pages with report-local cursors. It SHALL mark the snapshot as current, changed, or unknown. The job SHALL release a digest only after checking the complete report. The service SHALL verify the requesting owner's current authorization and connector access before releasing any report data. Partial, failed, or interrupted work SHALL expose no report or digest.

Managed report delivery SHALL work with governed erasures and concurrent disclosure receipt appends. An ordinary append SHALL preserve reusable verification of unchanged receipt evidence. A pending erasure or changed receipt evidence SHALL withhold affected report facts before release, including the summary and digest. Page delivery SHALL check current evidence for the paths it releases within the request deadline, even when the job processed an oversized referrer.

The paged report's approval digest SHALL bind all referrer outcomes and the pointer-write proof. Before applying a correction with that digest, the writer SHALL verify the same report against the current plan. A changed report SHALL refuse the correction before any write.

Restricted callers, and callers before a live observer is ready or without a live observer, retain the synchronous file preview. Its latency and behavior under concurrent file changes are unverified by this change.

A managed preview SHALL return `RECLASSIFICATION_PREVIEW_UNAVAILABLE` without counts, a job handle, or a digest when an owner-level rule or Access exclusion restricts the report. Before calculating counts or effects, the planner SHALL omit referrers withheld by their release decision or a governed erasure. Paged apply SHALL use the same release decision for its candidate scope. A change to the governed erasure set SHALL invalidate retained results.

When no values are supplied, evidence SHALL be limited to what is deterministically observable about the source: its current location, its recorded origin, its title, and its existing metadata. The operation SHALL NOT infer a classification through a model call, and SHALL report that it has no proposal rather than guessing when the observable evidence supports none.

#### Scenario: A preview writes nothing

- **WHEN** a reclassification is requested in read-only mode
- **THEN** the report names the destination and the affected references
- **AND** the source is unchanged at its original location
- **AND** no inbound reference has been rewritten

#### Scenario: A preview shows a refusal before approval

- **WHEN** a preview is requested for a source whose destination a page that the caller can see already occupies
- **THEN** the report states that the move would be refused and why
- **AND** it names that page

#### Scenario: Cold inbound evidence leaves the preview pending

- **WHEN** a served file-mode preview for an unrestricted owner with a live observer ready for background recovery cannot prove the inbound references
- **THEN** the response asks the caller to retry the same preview after the reported delay
- **AND** the response omits the reference count and approval digest
- **AND** a retry with complete evidence returns the normal preview

#### Scenario: The report fills several pages

- **WHEN** a managed owner previews a Source whose complete reference report exceeds the interactive bound
- **THEN** the response gives a job handle without an approval digest
- **AND** a concurrent Records read can finish while the job is running
- **AND** the completed job reports every outcome through bounded pages under one approval digest
- **AND** a change to a later referrer makes that digest stale before apply writes anything

#### Scenario: Governed report pages stay available across disclosure appends

- **WHEN** a completed report belongs to a vault with a governed erasure and a long valid receipt history
- **THEN** ordinary disclosure receipts can append while the owner reads every report page within its request deadline
- **AND** changing older evidence after disclosure recording withholds the result before the service releases it
- **AND** a pending erasure withholds the affected report before any referrer moves

#### Scenario: Evidence accompanies a proposed value

- **WHEN** a proposal is requested for a source whose current location already carries a domain segment
- **THEN** the proposed domain is reported together with the observation that supports it

#### Scenario: A caller previews the correction it has decided on

- **WHEN** a preview is requested with a kind the caller has judged from reading the source
- **THEN** the report names the destination that kind projects to
- **AND** the report states that the value came from the caller rather than from observed evidence
- **AND** the source is unchanged at its original location

#### Scenario: A previewed value is canonicalized, not echoed

- **WHEN** a preview is requested with a kind or domain in non-canonical form
- **THEN** the reported value is its canonical form
- **AND** the reported destination is the one that canonical value projects to

#### Scenario: An undecidable source is reported, not guessed

- **WHEN** a proposal is requested for a source whose observable evidence supports no particular kind
- **THEN** the report states that no kind is proposed
- **AND** no fallback value is presented as a proposal

## ADDED Requirements

### Requirement: Reclassification can be reverted

The operation SHALL offer a revert that restores the source's latest recorded previous path, kind and domain through the same atomic, reference-preserving relocation, and removes that history entry. The revert SHALL restore each append-only page that the reclassification repointed from its pointer-write record, and SHALL refuse, changing nothing, when such a page no longer holds its recorded after hash. Revert SHALL restore a recorded `other` classification. It SHALL refuse an entry whose classification is unknown, and a previous path that another page now occupies.

#### Scenario: A reclassification round trip restores the original

- **WHEN** a source is reclassified and then reverted
- **THEN** it is at its original location with its original kind and domain
- **AND** its body bytes and every reference resolve as before the first move
- **AND** each append-only page that the reclassification repointed holds its bytes from before the first move

#### Scenario: A legacy history entry cannot be reverted automatically

- **WHEN** revert is requested for a source whose only history entry came from a legacy scalar
- **THEN** the revert is refused with the reason
- **AND** nothing changes

### Requirement: A move follows the prose paths that name the moved file

The move operation SHALL rename the exact vault path of each moved file where a page body cites it in prose, unless wikilink updating is disabled. It SHALL find those pages through the maintained search catalogue as it stands, and SHALL NOT walk the vault or rebuild the catalogue. It SHALL change only a page body, never text inside a recorded origin carrier or a URL, and SHALL match whole paths only: a longer name beside the moved file is not the moved file.

The operation SHALL leave a Source or Evidence page that cites the path as captured, and SHALL NOT refuse a move because of such a citation. It SHALL report how many pages kept one. When the catalogue is not current, the operation SHALL say that path mentions were not checked rather than report none. A reclassification that relocates a source SHALL report its path mentions in the same way, in its preview and in its result.

Known limits: a mention written with Windows separators, or percent-encoded, is not recognised and keeps the old path.

#### Scenario: A note citing the path follows the move

- **WHEN** a mutable note cites a raw artifact by its vault path in prose and the artifact is moved
- **THEN** the note names the new path and the result counts the rewritten mentions

#### Scenario: A Source card citing the path is left as captured

- **WHEN** a captured Source card cites the artifact by its vault path in prose and the artifact is moved
- **THEN** the move succeeds and the card is unchanged
- **AND** the result warns that an append-only page kept a mention

#### Scenario: A longer path is not renamed

- **WHEN** a page names `<path>.bak`, a file whose name ends with the moved file's name, a sibling whose name begins with it, or the path inside a URL
- **THEN** that text is unchanged after the move

#### Scenario: An unanswered lookup is reported

- **WHEN** the search catalogue is not current during a move or a relocating reclassification
- **THEN** the write still applies, the catalogue is not rebuilt in the request, and the warnings state that path mentions were not checked

### Requirement: The audit reports a Source pointer that does not name its bytes

The audit SHALL offer the category `source_pointer_drift`. For a Source or Evidence card it SHALL check each structured pointer to its raw artifact (`evidence_file`, `data_file`, and the `artifact_path` of `raw_protection` and `governance_companion`) and report a pointer that names no file, leaves the vault, names a path that custody does not read as a plain file (a link, a reserved name or a folder), or names a file whose size differs from the recorded one. The check SHALL read file metadata only; content fixity stays with the original fixity pass. A card with no structured pointer SHALL be checked through the custody paths its body cites in a code span, where a trailing `:<line>` is a line citation and not part of the path. A healthy structured pointer SHALL end any finding about the card's prose.

Each finding SHALL name the card, the field, the recorded path and the problem, and SHALL name a candidate path only when exactly one same-named file exists in the recorded path's custody tree. Its proposed fix SHALL be one step that succeeds: a repoint through reclassification for a Source that does not follow its file by name, and otherwise a move of the candidate back to the recorded path. For a caller other than the owner, the check SHALL answer a file that the caller may not see as a missing one and SHALL NOT propose it as a candidate. One unreadable path SHALL be one finding and SHALL NOT stop the audit.

#### Scenario: A pointer left behind by a move names its candidate

- **WHEN** a card's `data_file` names a path that no longer exists and one same-named file exists under Sources
- **THEN** the audit reports the card with that candidate path

#### Scenario: Two same-named files propose nothing

- **WHEN** two files under Sources share the missing file's name
- **THEN** the finding names no candidate

#### Scenario: A card with only a prose path is checked

- **WHEN** a Source card has no structured pointer and its body cites a custody path that no longer exists
- **THEN** the audit reports the card, and reports nothing once a repaired pointer names an existing file

#### Scenario: A path that custody refuses is one finding

- **WHEN** a card's pointer names a link or a path with a reserved character, with or without a media store bound to the vault
- **THEN** the audit completes and reports that card's pointer as unreadable

#### Scenario: A card that cannot be repointed gets a move

- **WHEN** an Evidence card, or a card that sits beside its file under the file's companion name, names a missing file that has one candidate
- **THEN** the proposed fix moves the candidate back to the recorded path, and after that move the audit reports nothing

### Requirement: A Source's pointer to its bytes can be repaired and reverted

The reclassification operation SHALL accept `artifact_path`, a vault path to the raw file the card describes. It SHALL set every scalar pointer the card records, or `evidence_file` when it records none, and SHALL record the previous values in the new `moved_from` entry so that a revert restores them. A repair that names no kind or domain SHALL NOT move the card or change its classification.

It SHALL refuse a target that is not a vault-relative plain file under a custody tree, a target that does not exist, a target whose size or SHA-256 differs from the identity the card records (for an original the media store holds, the identity its binding records), and a card that follows its file by name: a pointer in a `raw_protection` or `governance_companion` descriptor, or a card at its file's companion path. For a caller other than the owner, it SHALL refuse a target that the caller may not see exactly as a missing one. A repair SHALL leave every classification field as the card states it.

#### Scenario: A repair is recorded and reverted

- **WHEN** a card's `evidence_file` names a missing path and a repair names the file that exists
- **THEN** the card names that file, and its history entry records the previous value
- **AND** reverting the entry restores the previous value

#### Scenario: A repair to a missing or different file is refused

- **WHEN** the named file does not exist, or its bytes differ from the recorded SHA-256
- **THEN** the repair is refused and the card is unchanged

#### Scenario: A withheld target reads as a missing one

- **WHEN** a restricted writer names a file that its scope withholds as the target of a repair
- **THEN** the refusal is the one a missing file gets, and the card is unchanged

### Requirement: A binary sidecar records that no kind was chosen

A sidecar card that the system writes for a binary artifact SHALL record the unclassified kind unless the artifact is a dataset export. It SHALL NOT record the retired `other` kind.

#### Scenario: A preserved binary is left for a person to classify

- **WHEN** a binary file is preserved with no kind chosen
- **THEN** its sidecar records the unclassified kind
