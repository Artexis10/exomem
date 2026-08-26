## Context

`replace(old_path, new content)` today: check old page not already superseded → `note()` writes the NEW page (commit 1) → inject `supersedes`/flip old `status: superseded` + `superseded_by` (commit 2) → append log (commit 3). Three separate commits, no CAS on the old-status read. Failure modes: (a) two concurrent replaces both pass the check and produce competing successors, the later old-page write dropping the earlier's pointer; (b) a crash after commit 1 leaves a standalone new page with a dangling `supersedes` and an un-flipped old page.

## Goals / Non-Goals

- Goal: a supersession is all-or-nothing across new page + old-page chain flip + log.
- Goal: concurrent supersession of the same active page → exactly one winner; the loser is refused (stale old page).
- Non-goal: multi-page transactions beyond a single supersession; reworking `note()`'s own write.
- Non-goal: changing the read/tombstone-demotion behavior of `find`.

## Approach (to be finalized by the implementer)

1. Capture the old page's `content_hash` at the status-read and require it unchanged through commit (optimistic concurrency): if the old page changed between read and write, refuse `REVIEW_ITEM_CHANGED`/`STALE_SUPERSEDE`.
2. Build ONE `batch_atomic_write` containing the new page, the old page's flipped frontmatter (status + `superseded_by`), and the log update, so staging is all-or-nothing and a mid-flip failure leaves the whole set unapplied (or cleanly re-raises without a dangling new page).
3. If `note()` cannot be folded into the same batch, restructure so the new-page content and the old-page flip are staged together and replaced together.

Open questions for the implementer: whether `note()`'s index/log side effects can be composed into the single batch, or whether a compensating cleanup is needed if the batch can only cover the two page files + log.

## Risks

- Folding `note()` into a single atomic batch may require refactoring how `note()` stages its writes; keep the change minimal and covered by the existing supersession tests.
- The CAS must not reject legitimate sequential re-reads (only genuine concurrent modification).
