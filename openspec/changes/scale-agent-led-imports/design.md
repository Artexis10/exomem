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

### D1. Only a recorded stage is consumable

A staged copy is consumable only when the stage is proven to be an import's own copy:

- **Cloud:** the vault-state receipt `cloud-imports/<import_id>.json` exists and names an `adopt` import whose staging path is this `_Imports/<import_id>/`.
- **Local:** `exomem import` writes a stage record `local-imports/<import_id>.json` in the vault's state folder when it claims the import id. The record holds the import id, the folder name and the record time. When the vault's state placement is not settled yet, the importer writes no record. That stage is then not consumable: it files with two copies, as today, and adoption says so in its next actions.

A folder that a person creates under `_Imports/` has no record, and adoption never consumes it. The run object stores the decision with its proof (`consume: {"stage": "<import_id>", "proof": "cloud-receipt" | "local-record"}`) at `start`. A run started with `consume=false` never consumes.

### D2. An item is consumed only once it is fully settled and verified

An item's staged copy is removed only when all of these hold:

- Its outcome is `applied` or `already-applied`. An item in `_SOURCE_ONLY` (a draft note still owed) keeps its staged copy, because the note step re-reads it.
- The chunk's batch has committed.
- The durable copy matches the planned `original_sha256`, read back after the commit by streaming:
  - for an artifact, the artifact file's bytes;
  - for a text item, the original text recovered from the committed Source page and hashed as the original bytes. If the Source's form cannot reproduce the bytes exactly, the text item is not consumed. See the open questions.
- The staged file still has the planned size and hash. This is the existing `_record_unchanged` check, taken before the unlink and kept as `original_unchanged`.

### D3. Crash-safe order

1. The outcome records `consume: "pending"` in the run object that the chunk's commit writes.
2. After the commit and the D2 checks, adoption unlinks the staged file through the held filesystem. It uses the same pattern as `media_store.evict`, which keeps the vault's access-tier backstop.
3. Adoption records `consume: "done"` at the next run save.

On replay, a `pending` item whose staged file is missing and whose durable copy verifies counts as consumed. A `pending` item whose staged file is present is verified and unlinked again. The repeat-call re-verification in `apply` and `probe_staleness` skip consumed items, so `verified_unchanged` keeps the hash taken before the delete. Empty folders left in the stage are removed at `finish`.

### D4. Free space is checked per chunk

Before each chunk, `apply` compares the chunk's bytes plus the vault's reserve against free space. When they do not fit, it stops the call with a resumable `INSUFFICIENT_SPACE`, giving the measured bytes, and a next action. Because consumption frees space as filing proceeds, the peak is about one copy plus one chunk. A large artifact adds its own size once, while both of its copies exist.

### D5. The inventory pages

`start(after=<vault-relative path>)` scans in the existing deterministic order and records rows strictly after the cursor, up to `max_candidates`. It returns `next_after`, the last recorded path, while more rows exist. Each page is its own run with the same collection label, so the import index dedupes across pages, as a repeat import does today. The cap stays: it bounds the run object that every chunk rewrites.

### D6. The staged-file ceiling is the session cap, read by streaming

Inventory eligibility and apply for an artifact use the upload-session cap (`upload_sessions.max_bytes_from_env`) on every runtime, instead of the client-pull cap. Apply hashes and copies an artifact in bounded chunks. Text items keep the existing text ceiling, because rendering needs the whole text in memory. A text file over that ceiling becomes an artifact item rather than an ineligible row.

### D7. The agent leads from tool text

- The `observe_transfer` description and its `import_handoff` routes name the full sequence: import, wait for `done`, page through `start`, `select`, `plan`, `apply` and `finish`, review the residual, then discard.
- `adoption_studio` `next_actions` name the next page while `next_after` is set.
- `finish` reports the residual left in the stage (unselected, ineligible and failed), with counts and bytes, and names the discard route.
- The skill scaffold gains a generic import reference.

A client without a shell is routed to Home, because it cannot read local files.

## Risks

- **A wrong consume deletes the only staged copy.** D1 and D2 restrict it to proven stages and verified durable copies. The person's own folder or device keeps the original bytes in every route, because both importers copy.
- **Links across pages.** Planning resolves links against planned items and earlier imports, so a link into a later page stays unresolved at plan time. See the open questions.

## Open questions for the critic

1. Can a text Source reproduce the original bytes exactly, given encoding, line endings and fence escaping? If not, should text items stay unconsumed? Text is a small share of bytes in media-heavy imports.
2. Should pages order artifacts before text, so that a note's embeds resolve against earlier imports? And should `finish` re-resolve links across the pages of one collection?
3. Is the local stage record better written at claim time or at finish? Is skipping it, when the vault's state placement is unsettled, the right fail-safe?
4. Does consuming interact with backup, Trash or media custody of the staged tree in a way that these decisions miss?
