## 1. Design + spec

- [ ] 1.1 Map `replace.py`'s current commit sequence and `note()`'s staging; decide whether the new page can join the old-page flip + log in one `batch_atomic_write`.
- [ ] 1.2 Finalize `specs/supersession-atomicity/spec.md` (expand scenarios: concurrent-replace loser refused, crash-mid-transaction leaves no dangling new page, sequential re-supersede of an already-superseded page still refused). `openspec validate harden-supersession-atomicity --strict`.

## 2. Implement

- [ ] 2.1 Add a compare-and-swap on the old page's `content_hash` across the supersession transaction.
- [ ] 2.2 Stage new page + old-page chain flip + log in ONE atomic batch (all-or-nothing).
- [ ] 2.3 On CAS failure, refuse with a clear stale error and write nothing.

## 3. Verify

- [ ] 3.1 New test: two concurrent `replace` calls on the same active page → exactly one successor chain, the other refused; no dropped pointer.
- [ ] 3.2 New test: an injected failure mid-transaction leaves NO standalone new page and no half-flipped old page.
- [ ] 3.3 `tests/test_replace.py`, `tests/test_supersession_surface.py` stay green.
- [ ] 3.4 `uvx ruff check` scoped to changed files only.
