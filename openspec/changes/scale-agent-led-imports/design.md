# Design: scale agent-led imports

## Context

The import routes are `exomem cloud import` (encrypted archive, a cell Job unpacks it into `_Imports/<import_id>/`) and local `exomem import` (a resumable copy into the same layout). Both end at `adoption_studio`, which files each item as a Source and keeps the staged copy until a discard. Peak disk use is therefore the staged copy plus the filed copy. On a Cloud cell the volume must hold both. The adoption inventory caps at `MAX_CANDIDATES_DEFAULT` (5,000) because every chunk rewrites the whole run object (`AdoptionRunStore.save`). The eligibility ceiling is `client_artifacts.fetch_budget().max_file_bytes`, which on a Cloud cell is the gateway's client-pull cap of 100 MiB, even for a file that is already on the cell.

## Goals

- Filing a staged import needs about one copy of the data plus one chunk and one large file, not two copies.
- A run can cover any number of candidates.
- A staged file over 100 MiB adopts on Cloud as it does locally, with bounded memory.
- An agent can lead the whole flow from tool text alone.

## Non-goals

- Imports larger than the volume: `add-cloud-media-store` task 5.4.
- The Substrate admission factor: `publish-cloud-import-limits` follow-up.
- Changing what a Source or artifact contains.

## Decisions

The independent critique of the first draft found no route to byte loss: every consume follows a hash match against the Knowledge Base copy. It found four wrong results, in crash bookkeeping, local resume, page identity and link resolution. These decisions settle them.

### D1. Only artifacts in a recorded stage, and only listed files, are consumable

A run consumes only artifact items. Text items, which are mostly Markdown notes owed draft notes, stay staged until discard. Text is a small share of bytes, and a text file over `INLINE_TEXT_MAX_BYTES` already imports as an artifact. Consuming text would also need fence-level verification, would interfere with the owed-note step, and would make the file watcher publish deletions for every note.

A stage is recorded when its own manifest lists each staged file with its path, size and SHA-256:

- **Cloud:** the import Job adds a member manifest to its vault-state receipt `cloud-imports/<import_id>.json` for an `adopt` import.
- **Local:** `exomem import` writes a stage record `local-imports/<import_id>.json` in the vault's state folder when the import finishes, from its journal. An open import is not recorded, so a rerun never has to recopy a consumed file. When the vault's state placement is not settled, the importer writes no record and reports that adoption keeps the staged copy.

At removal time, the staged file's stage-relative path, size and SHA-256 must match a manifest entry. A file that a person or a sync tool adds to a stage later is never consumed. A stage under `_Imports/` without a manifest, a restored stage (a restore moves the state folder aside), a moved vault, and discard all leave nothing consumable. A run started with `consume=false` never consumes.

### D2. Order, verification and recovery

After the chunk's batch commits, adoption removes an artifact item's staged copy when all of these hold:

- the outcome is `applied` or `already-applied`;
- the durable artifact matches the planned `original_sha256`, from the batch's write-time hash for a fresh write and a streamed read-back for an already-applied item;
- the D1 manifest proof holds.

The removal uses `held_fs.unlink` with an identity check under the held handle.

The committed Source is the recovery record, not a flag in the run object. When an artifact item's staged copy is missing in a recorded stage, `_read_original`'s caller resolves it through the import index (`_earlier_with_same_bytes`, matching collection, `collection_path` and `original_sha256`) before it reports a failure. This covers every path:

- a stop after commit and before the run object is saved;
- the settled and dedupe paths;
- a batch that raised after it landed;
- `retry_failed`;
- a concurrent run on the same stage.

Repeat-call re-verification and `probe_staleness` skip items whose staged copy is gone and whose Source verifies. A failed removal leaves the item applied and not consumed, never failed. The run reports a `consumed` count separately from `verified_unchanged`. `finish` removes empty stage folders and reports the residual (unselected, ineligible, failed and text items) with counts and bytes, and the discard route.

### D3. Free space is checked per chunk

Before each chunk, `apply` compares the chunk's bytes plus the reserve against free space. When they do not fit, it stops with a resumable `INSUFFICIENT_SPACE` that gives the measured bytes. The reserve must cover derived state: the index, embeddings, the graph and media sidecars.

### D4. The inventory pages with one collection identity

`start(after=<cursor>)` records rows strictly after the cursor in the scan's deterministic order, up to `max_candidates`, and returns `next_after` while rows remain. The cursor is an opaque token that carries the collection root and label pinned by the first page, so consuming or removing an empty folder cannot change them. Every page is its own run under that identity, so the import index dedupes across pages. Pages order artifacts before text, so a note's embeds resolve against earlier imports.

Link resolution on a paged collection uses the full stat-only walk that `build_inventory` already performs before truncating:

- a link resolves by exact path, or by a bare name that is unique across the whole walk;
- a match by alias only stays unresolved, because later pages' aliases are unknown.

`plan` refuses a page while an earlier page of the same collection still has unapplied plan items, so destinations cannot collide. The cap stays, because it bounds the run object that every chunk rewrites.

### D5. Large staged artifacts get their own ceiling and one streamed pass

A new `adoption_ceiling()` sets the staged-file ceiling on every runtime. `fetch_budget()` stays the client-pull cap, because `server_transfer` and `upload_handoff` use it.

An artifact is copied in a single streamed pass that hashes while it copies and rolls back on a mismatch. This replaces the separate pre-hash, copy and post-hash passes, so a large file costs one read and one write. The ceiling defaults to what fits one call's time budget on a Cloud volume. A larger file stays in the residual with a code that names it. Background apply for files above one call is a follow-up.

### D6. The agent leads from tool text

The `observe_transfer` description, its `import_handoff` routes and the `adoption_studio` next actions name the whole flow: import, wait for `done`, page through `start` while `next_after` is set, `select`, `plan`, `apply`, `finish`, review the residual, then discard. The skill scaffold gains a generic import reference. A client without a shell is routed to Home.

### D7. Proof runs where admission does not double the volume

Substrate admission still multiplies declared bytes by two. On attachable storage, a cell grows its volume or refuses the import. A local-storage cell publishes no limits and keeps legacy admission without growth. The rehearsal therefore proves one-copy filing on a local-storage cell with a fixed volume below twice the data. Making the factor depend on the import kind remains the `publish-cloud-import-limits` follow-up, and attachable cells near the cap need it.

## Risks

- **A wrong consume deletes the only staged copy.** D1 and D2 restrict it to manifest-listed artifacts in a recorded stage, verified against the Knowledge Base copy. The person's own folder or device keeps the original bytes in every route, because both importers copy.
- **Links across pages.** D4 leaves alias-only matches on a paged collection unresolved rather than guessed.
