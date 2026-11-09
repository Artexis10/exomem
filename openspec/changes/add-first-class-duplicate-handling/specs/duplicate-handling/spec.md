## Purpose

Keep every original and every captured text in the vault once. Detect exact duplicates when content is written or imported, report the ones that exist, and fold them through reviewed, undoable batches. A duplicate is identical bytes and nothing near.

## ADDED Requirements

### Requirement: A duplicate is identical bytes, nothing near

The system SHALL treat two items as duplicates only when their stored bytes are identical. For an original file (an Evidence artifact, a Source artifact or an imported file) the bytes are the file's bytes. For a text Source they are the captured text as the Source stores it, encoded as UTF-8, outside its frontmatter, title heading, why-captured quote and generated sections. Identity SHALL be compared by SHA-256 within one lane (Sources or Evidence) and one protection class (owner-only or not). A title, URL, filename, embedding, wording or any other similarity SHALL NOT make two items duplicates. The near-duplicate advisory that capture already gives SHALL stay an advisory and SHALL NOT reuse, link or fold anything.

#### Scenario: One photo under two filenames

- **WHEN** two Evidence artifacts have identical bytes and different filenames
- **THEN** they are duplicates of each other

#### Scenario: Two captures differ only in title

- **WHEN** two text Sources hold identical captured text under different titles
- **THEN** they are duplicates, because the title is not part of the payload

#### Scenario: Reworded text is not a duplicate

- **WHEN** two text Sources differ by one character of captured text
- **THEN** neither reuse, nor audit grouping, nor a fold treats them as duplicates

#### Scenario: The same bytes sit in both lanes

- **WHEN** identical bytes exist as a Source artifact and as an Evidence artifact
- **THEN** no write door reuses one for the other and no fold retires either
- **AND** the audit reports the group as spanning lanes, with no fold offered

#### Scenario: A protected copy and an unprotected copy

- **WHEN** identical bytes exist once owner-only and once not owner-only
- **THEN** they are not duplicates of each other

### Requirement: A derived hash index finds candidates and is never authority

The system SHALL keep a derived, rebuildable index from payload SHA-256 to the live pages that hold that payload, in state the vault can discard and rebuild from its canonical pages. It SHALL hold the payload hash of each live Source and Evidence page beside the content hash that the lexical catalogue already keeps per page. Every consumer SHALL re-hash the stored original, or re-derive the stored payload, before it treats an index hit as a duplicate. A hit whose bytes differ or cannot be read SHALL count as no hit.

The commit that stores a new original or Source SHALL record its payload hash in the index before the mutation boundary of that commit is released. The next acquisition of the boundary SHALL see it, whatever other derived indexes have caught up. A Source page written before this capability SHALL be indexed from its stored payload and SHALL NOT gain a recorded hash field.

When the index is missing or cannot be proven complete, the system SHALL rebuild it from canonical pages. While it cannot, lookup SHALL report unavailable. A write door SHALL then store the content as new and carry the warning `duplicate_check_unavailable`, and the audit SHALL report its duplicate categories as unchecked. Failing open is deliberate: a missed duplicate costs one extra copy that the audit reports, while a wrong hit would cost content. No door SHALL scan a content folder to find duplicates.

#### Scenario: An indexed original changed on disk

- **WHEN** the index maps a hash to a path whose bytes no longer hash to it
- **THEN** a capture or preservation of that hash stores new content and reuses nothing

#### Scenario: Other derived indexes lag

- **WHEN** a Source was committed a moment ago and search and graph indexing have not caught up
- **THEN** an identical capture in the next boundary acquisition finds it and reuses it

#### Scenario: The index is deleted

- **WHEN** the index is removed and a capture of already stored bytes arrives
- **THEN** the system rebuilds the index from canonical pages, finds the stored copy and reuses it
- **AND** no page of the vault changes because of the deletion or the rebuild

#### Scenario: The index cannot be completed

- **WHEN** the rebuild cannot finish
- **THEN** the capture stores its content as new with `duplicate_check_unavailable`
- **AND** the audit reports `duplicate_content` as unchecked, never as zero findings

### Requirement: Lookup and write share one mutation boundary

Every door that stores originals or Source payloads (capture, preservation, adoption and import) SHALL look up duplicates and commit its content, its update of a reused page or its reuse answer within one acquisition of the vault mutation boundary. Retrieval and staging that run before the boundary SHALL NOT decide reuse. Replay of an adoption key SHALL find an existing copy through the index and not through a folder scan.

#### Scenario: Two identical hand-overs arrive together

- **WHEN** two calls with different request identities hand over identical bytes at the same moment
- **THEN** exactly one original and one page are stored
- **AND** the other call reports the stored copy as `already_stored` or reused

### Requirement: Reuse never discloses content the caller cannot read

An index hit SHALL count only when the caller can read the existing page in full and, when reuse adds a family or an import mapping, can write that page. Otherwise the hit SHALL read as absent, the door SHALL store the content as new, and the answer SHALL equal the answer of a vault without that page. A protected copy SHALL never answer for an unprotected request, nor the reverse.

#### Scenario: The only copy is withheld from the caller

- **WHEN** a restricted caller hands over bytes whose only stored copy lies in a scope the caller cannot read
- **THEN** the call stores its own copy and its result does not reveal that another copy exists

### Requirement: The audit reports exact duplicates through two registered categories

The audit SHALL report `duplicate_content`: one finding per group of live Source or Evidence pages whose payloads are identical by re-hash. It SHALL report `duplicate_source_url`: one finding per group of live Sources that share one canonical URL and whose payloads differ. Both categories SHALL be declared through the same category registration as every other audit category, whose names select implemented checks and are not vault vocabulary. Both SHALL be selectable through `audit` and `attention`, SHALL stay out of the default `attention` union because a vault meets its whole backlog on the first run, and SHALL be advisory, never blocking.

A `duplicate_content` finding SHALL name each member (ref, path, lane, families or import mappings, recorded capture date), the suggested canonical page, the bytes that a fold would free, and the exact `fold-duplicate` step for each copy. A group that spans lanes, or that is raw-protected, SHALL say so and offer no fold. The suggested canonical page SHALL be the oldest filing, except that a member cited by a page that the fold cannot rewrite (an append-only page, or a page in another owner's tree) SHALL be the canonical page instead, and a group with two such members SHALL offer no fold and say why, so that a suggested step never meets a refusal at proposal. A `duplicate_source_url` finding SHALL name the Sources, say that their bytes differ, and offer no fold. A group of Sources with one URL and identical payloads SHALL appear once, as `duplicate_content`. Findings SHALL obey the caller's read admission, so a page the caller cannot read reads as absent. The report SHALL state its counts by lane and whether each count was re-verified by re-hash or read from the index alone, and an incomplete index SHALL read as unchecked.

#### Scenario: A group of three identical photos

- **WHEN** the audit runs over a vault where one photo is stored in three Evidence families
- **THEN** it returns one `duplicate_content` finding with three members, one suggested canonical page and two fold steps

#### Scenario: A copy that a Source cites stays

- **WHEN** the audit runs over two identical Evidence pages and a Source cites the newer one
- **THEN** it suggests the newer page as the canonical page
- **AND** the `fold-duplicate` step it carries passes proposal unchanged
- **AND** a group with two pages that such citers cite offers no fold and gives the reason

#### Scenario: A stale index entry

- **WHEN** an index entry groups two pages whose bytes no longer match
- **THEN** the audit returns no finding for them

#### Scenario: One URL captured twice with different text

- **WHEN** two live Sources share a URL and differ in payload
- **THEN** the audit returns one `duplicate_source_url` finding that names both and offers no fold

#### Scenario: The categories do not flood the daily surface

- **WHEN** `attention` runs without a category filter over a vault that holds many duplicate groups
- **THEN** no duplicate finding appears
- **AND** `attention` with `categories=["duplicate_content"]` returns them

### Requirement: A fold retires one copy and keeps everything it carried

A fold SHALL retire one copy in favour of one canonical page whose payload is identical, and SHALL run only through the `fold-duplicate` curation step. At proposal, preview and apply the system SHALL re-verify that both pages are live, are in the same lane and protection class, and hold payloads that re-hash identical, and that the canonical payload is readable. A fold across lanes, across protection classes, or between pages whose bytes differ SHALL be refused.

The fold SHALL:

1. Merge into the canonical page's frontmatter the copy's evidence families, import mappings and `ingested_into` back-references.
2. Rewrite each inbound reference to the copy in other live pages so that it names the canonical page.
3. Mark the copy's page `status: superseded` with `duplicate_of` and `superseded_by` naming the canonical page, through the writer that marks recap revisions superseded.
4. Move the copy's original bytes to the vault trash. A text Source has no separate bytes, so its marked page stays as the whole copy.

No other change SHALL occur. The canonical page's payload and original bytes stay byte-identical. The choice of canonical page is the agent's judgement, shown in the preview. The work item SHALL suggest the canonical page that the audit requirement above defines (the oldest filing, by recorded capture date and then path, unless a citer pins another member), and the system SHALL NOT choose.

The fold is the only way to retire a Source or Evidence page. `edit_note`, `replace`, `supersede`, `move` and `delete` SHALL keep refusing Sources and Evidence.

#### Scenario: A copy folds into the oldest filing

- **WHEN** a plan folds an Evidence copy into its canonical page
- **THEN** the canonical page lists both families and both import mappings
- **AND** pages that linked to the copy now link to the canonical page
- **AND** the copy's page is marked superseded with `duplicate_of`, and its bytes are in trash
- **AND** the canonical page's original bytes are unchanged

#### Scenario: Bytes differ at apply

- **WHEN** either page changed between preview and apply so that the payloads no longer match
- **THEN** the step is refused as stale and writes nothing

#### Scenario: Other tools still refuse Sources and Evidence

- **WHEN** an agent calls `delete` or `supersede` on a Source or Evidence page
- **THEN** the call is refused exactly as before this capability

### Requirement: Folds run in reviewed batches

Each fold batch SHALL be one curation plan that the agent proposes from audit findings, that the system previews in full, and that runs only after the owner confirms that exact plan. A batch SHALL stay within the existing step cap of a plan. No fold SHALL run automatically, in the background or on a schedule. The owner confirms because choosing which filing is canonical and retiring the others is the owner's decision, which no rule can make for them. Every applied batch SHALL keep its binding manifest so the existing compensation plan can undo it.

#### Scenario: The audit alone changes nothing

- **WHEN** the audit reports duplicate groups
- **THEN** no page, original or index entry of the vault changes until an owner confirms a fold plan
