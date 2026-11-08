## Context

One read-only measurement of a personal vault (release 0.165.0) gives the problem its size:

| What | Count |
|---|---|
| Byte-identical extra Markdown pages outside the trash | 0 |
| Groups of identical binary originals | 79, with 121 extra copies |
| Groups that span folders | 67 (photos 40, one import 31, product assets 16, recordings 13) |
| Evidence page pairs with identical extracted text | 7, each 8 to 40 KB |
| Live Evidence title collisions | 64, all one filename filed in two folders |
| Recap files for episodes | 5,151 files for 861 episodes |
| Superseded recap revisions | 4,290 |
| Groups of recaps with one title | 339, all single-episode chains |
| Citations of superseded revisions | 44, from 32 compiled pages, to 36 revisions |
| Groups of Sources that share a URL | 56, with 76 extra |

The numbers show two different problems.

- **Copies of originals.** Photos, recordings and assets exist in several folders. Each door checks only its own narrow case. Evidence preservation checks one destination folder. Import checks one original path. `capture_source` checks nothing. The vault holds no byte-identical extra Markdown page, so an exact-duplicate problem for Markdown does not exist yet. The Source payload check keeps it that way.
- **Revision files.** Each recap revision is a new file. Older revisions get a frontmatter mark (`status: superseded`). The large majority of the "duplicates" a title search finds are these revisions, not copies.

Existing mechanisms this change builds on:

- `preserve.find_duplicate_artifact` and `destination_duplicate_index`: exact SHA-256 check in one destination folder, with a re-hash of the stored artifact.
- The `already_stored` outcome, with `duplicate_of`, of the artifact hand-over.
- `client_artifacts._source_duplicate`: a scan of `Sources/<kind>` per adoption. It costs about 1.1 s at 3,000 pages.
- `add._supersede_writes`: the frontmatter mark for superseded recaps.
- `compiled_history`: create-only history entries under `Knowledge Base/_Compiled/history/`, with no rotation, readable through `read_memory(include_history=true)`.
- The curation lane: a closed set of step kinds, a binding manifest, witnesses and a compensation plan.
- The lexical catalogue, which keeps a `content_hash` per page.

The earlier decision to scope the Evidence check to one destination folder rested on one reason. A global content address would hide the second filing of a fact. The owner has now decided the other way: bytes are stored once and one page lists every family. This change reverses the earlier decision on purpose.

## Goals and non-goals

Goals:

- New exact duplicates stop being created, through every door.
- Existing duplicates are reported and folded through reviewed, undoable batches.
- One episode is one page. Its history stays out of notes and search and is kept without limit.
- The duplicate check is race-free and costs one index lookup.

Non-goals:

- Near-duplicate merging. A model judges "near". The existing advisory stays as it is.
- Cross-vault consolidation (`add-governed-vault-consolidation`).
- Retitling compiled notes that share a title. This change adds no title-collision audit. Ordinary curation handles them.
- Folding duplicate own-notes that adoption of a collection creates. One draft per collection path stays.
- Archive member pools. They stay per family.
- Live folds of a real vault. They are separate, owner-confirmed operations after release.

## Decisions

### D1. Identity is exact SHA-256 over a lane-specific payload

Identity uses the original file bytes for an original. It uses the captured text, as stored and outside frontmatter, for a text Source. It excludes title, URL, filename and every similarity. Why: an exact hash is a fact. Anything near is a judgement, and a word list or regex must never decide meaning (C6).

### D2. Reuse and fold work per lane and per protection class

Identical bytes in a Source and in an Evidence page, or owner-only and not owner-only, are not duplicates. The audit reports a cross-lane group and offers no fold. Why: each lane has its own contract, and an owner-only copy that answered for an unprotected request would disclose content.

### D3. The index is a derived column beside the lexical catalogue's `content_hash`

The index maps payload SHA-256 to live pages. It lives as a payload-hash column in the lexical catalogue, next to `content_hash`. Why: the catalogue already keeps one derived row per page, and a second store adds a second place to keep in step. This home stays open to the objection of the owner of the planned single SQLite store. If that owner objects, the index moves into that store as one table with the same contract. The requirements do not name the home.

The index never decides. Every consumer re-hashes the stored bytes before it treats a hit as a duplicate. Why: a stale or damaged index must cost a missed reuse, never a wrong one.

### D4. The commit records the hash before it releases the boundary

The index is available at the next boundary acquisition, whatever other derived indexes have caught up. Why: the race that the #2537 follow-up names is the duplicate check running outside the writer guard. A check that sees a lagging index would store twice again.

### D5. The index fails open

When the index is missing, the system rebuilds it from canonical pages. While it cannot, a write door stores new content with `duplicate_check_unavailable`, and the audit reports its categories as unchecked. Why: a missed duplicate costs one extra copy that the audit finds later. A refusal would cost every write while a derived index repairs itself. Unknown stays visible and is never zero.

### D6. Identity-bearing captures are exempt from payload reuse

A capture that carries a caller identity (`source_identity`) is decided by that identity alone. Why: the caller has said which fact this is. Two facts with the same text but different identities are two Sources, and the existing identity rules already cover replay.

### D7. Reuse writes nothing, merges nothing and names what it did not apply

A capture whose payload is already stored returns the earlier Source with `capture_outcome: "reused"` and `duplicate_of`. The earlier Source stays unchanged. The result lists each caller-provided field that differs from the earlier Source (URL, title, tags or other metadata) in `fields_not_applied`, so nothing is dropped silently. Why: the Source is append-only and the caller can add a link or note on top. A merge would need a Source write, which is the one thing the lane refuses. A shared URL with different text is the opposite case: it writes a new Source and returns a `same_url` advisory with up to five named earlier Sources and a count. Why: the text is new content, and the skill already promises to surface a repeated URL.

### D8. One Evidence page lists every family

Identical bytes are stored once in the vault. The first page keeps its original and gains a `families` list. A later hand-over into another family adds that family to the list through a guarded frontmatter-only write, and returns `already_stored` with `family_added`. Listing and filtering read the list. A page without a list takes its folder family first. Why: the owner decided "one page, all its families". The `already_stored` name stays, so existing callers keep working.

### D9. Import records every source path and writes nothing twice

Adoption checks bytes against the vault through the index and against earlier items of the same plan. A repeat becomes a `duplicate_of` item. It adds an `also_imported_from` entry to the existing page through one frontmatter-only write, and it writes no Source and no artifact. A re-run finds the recorded mapping and writes nothing. `also_imported_from` counts as an earlier import for the replay check. Why: the owner of the import path asked for one original, every source path on the page, and a re-run that writes nothing.

### D10. Which copy is canonical is the agent's judgement, shown in the preview

The audit suggests the oldest filing, by capture date and then path. The system never chooses. Why: the choice carries meaning (which family or collection the owner considers primary), so no rule can make it. The owner confirms each batch.

### D11. A fold has its own leaf and its own invariant

The two fold kinds are new leaves of the curation lane. They are the only way to retire a Source or an Evidence page. The refusal of `move`, `delete` and `recover` stays, because the lane still has no proof of relocation history. A fold leaf moves a file to the trash only after it re-verifies, just before the move, that identical bytes remain held elsewhere: the canonical page's original, or the history entry. Why: with that invariant an uncertain rename cannot lose content, so the fold needs no namespace lineage. Resume decides from live state and blocks with `CURATION_OUTCOME_UNCERTAIN` when the state is unclear.

Fallback, if the owner of the lane prefers that no step moves a file: the two fold kinds merge, mark and write history only, and the existing post-seal trash tail moves the files. The vocabulary is ten kinds either way. The cost is a second apply stage that has its own preview. This is an open question.

### D12. A recap revision is rewritten in place with history

The recorder rewrites the episode's page and creates one compiled-history entry that holds the exact previous bytes, in one atomic write. A failed history write fails the revision. Why: the owner asked to keep history for good, and the compiled-history store already has no rotation and stays outside notes, titles and search. Episode pages become a new parent kind of that store. The parent set belongs to `semantic-write-contract`, so this change widens it there as a modification. The recap requirements only refer to it.

Only the episode recorder may rewrite a Source. Its target check is closed: a recap Source in the episode folder, with the caller's `episode` key and filename group. Why: the append-only guard must stay for every other writer, and a general exception would reopen it.

### D13. Coverage resets on a revision that changes Decided lines

A recap revision that changes the Decided lines clears the head's `ingested_into` back-references. Why: a note that cited the old text has not ingested the new Decided lines. Without the reset the audit would claim coverage that does not exist. A revision that keeps the Decided lines keeps the references. Citations of the text the page held when cited stay resolvable through the pinned revision files and history. This default is a question to the owner.

### D14. A recap fold pins every referenced revision over the whole vault

Proposal computes the pinned set from `sources:` citations, `ingested_into` back-references and links of every live page outside the episode, whatever its lifecycle class and regardless of what the caller can see. It refuses to run without whole-namespace content authority. Why: a caller that sees fewer pages could retire a revision that a hidden page cites. In the measured vault this keeps 36 revisions as files and moves about 4,254 to history. Pinned revisions stay as files with their mark, so every one of the 44 citations resolves to the text it cited, before, during and after a fold.

A history entry holds the exact stored bytes of a revision, not a derived form. Why: the entry is the only copy once the file moves to the trash, and byte equality is the check that lets the move happen.

### D15. The audit categories are opt-in

`duplicate_content` and `duplicate_source_url` register through the existing category registry. They stay out of the default `attention` union. Why: the first run meets the whole backlog (121 extra copies and 76 extra Sources in the measured vault), and a daily surface that starts with that backlog trains its reader to ignore it. They are advisory and never block.

`duplicate_source_url` offers no fold. Why: the texts differ, so nothing is a duplicate. The owner may link or supersede through the normal tools.

### D16. Mapping writes use the existing guarded frontmatter write

Adding a family, an import mapping or a back-reference uses a content-hash-guarded, frontmatter-only write of an existing page. Why: no new write path means no new way to lose a page. The write never touches the body or the original bytes.

## Risks and trade-offs

| Risk | Handling |
|---|---|
| The index is stale after an out-of-band edit | Every hit is re-hashed. A stale hit reads as no hit. A rebuild repairs the index from pages. |
| The index home is rejected by the single-store owner | The contract names no home. The index moves as one table. |
| A recap revision silently changes what an old citation claims | Pinned revision files and the coverage reset (D13). A citation holds the text the page held when cited. |
| The `ingested_into` reset makes older notes show as uncovered | The audit reports an uncovered Decided line as it does today. This is the honest state. It is a question to the owner. |
| The journal path of a trashed revision goes stale | Resume decides from the recorded trash identity. The trash move happens only after history read-back. |
| Undo trashes history entries that a fold created | Compensation removes only the entries that the manifest names, and only while the revision file is restored. This conflicts with "kept without limit" in one case: an undone fold. It is a question to the owner. |
| Adoption of a collection creates duplicate own-notes | Out of scope. One draft per collection path stays. The audit does not group notes. |
| A reused capture does not apply a different URL or title | The reuse answer names the earlier Source and each field it did not apply. The caller can link or add a note. |
| The audit costs time on a large vault | It reads the index and re-hashes only members of groups of two or more. |
| A fold of a wrong canonical choice | The preview shows it, the owner confirms, and compensation restores it. |

## Migration and undo

- Upgrade changes no page. The index builds from canonical pages on first use. Old Source pages gain no hash field.
- A vault with the earlier recap layout keeps working. `record` revises the live page in place and leaves old revision files alone.
- Existing duplicates and revision files move only through owner-confirmed fold batches. The curation lane keeps each batch's manifest and witness.
- Undo of a fold runs through the existing compensation plan. It restores the copy's page and original bytes, the canonical page's frontmatter and each rewritten inbound page. It removes the history entries that the fold created. The result is byte-identical to the state before the fold.
- Undo of the code change: the new frontmatter fields (`families`, `also_imported_from`, `duplicate_of`) are additive. An older release ignores them. The index column is derived and can be dropped.

## Outcome check

The check runs on a disposable copy of the measured vault, never on the vault itself.

1. Run the audit. Expect the counts above: 0 extra Markdown pages, 79 identical-binary groups with 121 extra copies, 7 Evidence pairs and 56 shared-URL groups.
2. Time one adoption of a collection with the index and with the earlier scan, on the same copy.
3. Apply one recap-fold batch. Verify that all 44 citations resolve before, during and after, and that history reads back through `read_memory` with `include_history=true`.
4. Apply one duplicate-fold batch. Verify merged families, rewritten inbound references, the marked copy and the bytes in the trash.
5. Compensate both batches. Verify that every file of the copy is byte-identical to its state before the batches.

A live fold of the real vault needs its own disposable check, an owner confirmation that names the batch and a kept manifest. It is not part of this change.

## Open questions

These choices are the packet's own and need the owner's view:

1. Fold leaf with a trash move (D11), or sealed steps with the post-seal trash tail.
2. Coverage reset on a Decided-changing revision (D13), and how the audit should show it.
3. Exemption of identity-bearing captures (D6).
4. Reuse and fold per lane and protection class (D2).
5. No fold for `duplicate_source_url` (D15).
6. Payload reuse applies none of the call's fields and names each differing one (D7).
7. "Re-import after a rename". A moved staging root maps by relative path and writes nothing. A rename of one file inside the collection is a new path with identical bytes. It records a new mapping and writes no second original.
8. Opt-in audit categories (D15).
9. One own-note draft per collection path stays (non-goals).
10. Undo removes the history entries that its fold created.
