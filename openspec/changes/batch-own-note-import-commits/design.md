## Context

Draft notes have a `pending` status class, so they take the structural path of the semantic writer: no relation-review artifact, no draft hash and no log provenance. That path commits one page, its Source back-references, the index updates and one log entry in one `batch_atomic_write`. A chunk of own notes repeats that batch once per note.

## Decision

Commit the whole chunk through the same structural path in one `batch_atomic_write`, and keep every per-note guarantee inside it.

- Each draft is still validated and bound to its own draft token one at a time.
- `commit_creations` runs the Source closure and back-reference planning of each note inside one `mutation_guard`. It refuses two notes that plan the same file.
- Each note's contract verdict is evaluated against the base corpus plus every candidate of the chunk. A stale validity stamp refuses the batch.
- Navigation is planned once for all notes: one Recent-activity bullet and one log entry per note, in one write each.
- The vocabulary gate receives a tuple of sealed manifests, one per primary page.

The batch refuses a creation that needs the full relation review, that has a predecessor, or that binds a vocabulary entry. Those commit alone. The adoption caller falls back to the per-note path when the batch raises, so a refusal never changes an item's outcome.

## Not chosen

- Per-chunk navigation with per-note commits keeps the per-commit batch cost. It also separates a note from its index and log entries, which breaks note, index and log atomicity.
- Relation-reviewed batches need a review artifact for each note. Draft notes do not use one, so the batch stays on the structural path.

## Relation to `batch-governed-source-compilation`

That change keeps cross-note atomicity a non-goal for agent batches, because each agent note carries its own relation review and a later failure must not erase an earlier committed note. This change differs on purpose. The notes of one import chunk carry no relation review, the server groups them, and `batch_atomic_write` rolls the chunk back on any failure. A hard kill during installation can still leave a subset of the chunk. The identity check records those notes as already applied.

## Risks

- The write advisory of a multi-page batch is `not_required`. Only the first note of a call received one under fast acknowledgement.
- A chunk that introduces a new project key or topic registers it for the first note only. Notes that share that registration refuse the batch and fall back once.
