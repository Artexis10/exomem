## Context

Draft notes have a `pending` status class, so they take the structural path of the semantic writer: no relation-review artifact, no draft hash and no log provenance. That path commits one page, its Source back-references, the index updates and one log entry in one `batch_atomic_write`. A chunk of own notes repeats that batch once per note.

## Decision

Commit the whole chunk through the same structural path in one `batch_atomic_write`, and keep every per-note guarantee inside it.

- Each draft is still validated and bound to its own draft token one at a time, before the guard, as the per-note path does.
- `commit_creations` plans each note's Source back-references before the guard. It refuses two notes that plan the same file.
- Before the guard, each note's contract verdict is judged against the corpus that its preflight read plus every candidate of the chunk. The judgment runs only while every validity stamp is current.
- Inside one `mutation_guard`, the batch re-checks each note's Source closure and validity stamp, then writes. On a stale stamp it rebuilds the corpus once and judges every candidate again, as a single creation judges itself again.
- Navigation is planned once for all notes: one Recent-activity bullet and one log entry per note, in one write each.
- The vocabulary gate receives a tuple of sealed manifests, one per primary page.

The batch refuses a creation that needs the full relation review, that has a predecessor, or that binds a vocabulary entry. Those commit alone.

The batch raises only while nothing of it is on disk: a refusal, or a write error after the write rolled back. Only then does the adoption caller fall back to the per-note path, and it logs the refusal code once. After the write the batch never falls back, because each note would find its page taken. Each note is reported as created, and a step that fails after the write fails only the note it ran for, as in the per-note path. A failure that belongs to the whole batch, such as the catalog publication or a write that kept workspace state, is every note's outcome.

## Not chosen

- Per-chunk navigation with per-note commits keeps the per-commit batch cost. It also separates a note from its index and log entries, which breaks note, index and log atomicity.
- Relation-reviewed batches need a review artifact for each note. Draft notes do not use one, so the batch stays on the structural path.

## Relation to `batch-governed-source-compilation`

That change keeps cross-note atomicity a non-goal for agent batches, because each agent note carries its own relation review and a later failure must not erase an earlier committed note. This change differs on purpose. The notes of one import chunk carry no relation review, the server groups them, and `batch_atomic_write` rolls the chunk back on any failure that it catches. A hard kill is not caught; see Risks.

## Risks

- A hard kill while `batch_atomic_write` installs the chunk can leave part of it, for up to N notes instead of one. The write installs the index and log before the pages, so the index and log can name notes whose pages are missing. The next run creates those notes again and adds their entries a second time. The identity check records a note whose page was installed as already applied.
- The write advisory of a multi-page batch is `not_required`. Only the first note of a call received one under fast acknowledgement.
- A chunk that introduces a new project key or topic registers it for the first note only. Notes that share that registration refuse the batch and fall back once.
