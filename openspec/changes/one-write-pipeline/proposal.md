## Why

Exomem writes owner data through many surfaces: note tools, `save_source`, `save_evidence`, `save_episode`, Records and Planning, device uploads, imports and maintenance jobs. Each validates its input in its own way. Checked against main at 26ba98b9a:

- **Validation can pass what commit refuses.** Note validation returns before it plans the log, navigation index and auxiliary writes. The commit then checks every planned write, so a caller can pass validation and get `WRITE_REFUSED` at commit. Planning had the same defect for log-rotation archives (fixed in ca48e5798).
- **Validation and commit run different code.** The note tools validate in their own leaf. `save_source`, `save_evidence`, the episode store and device intake have no validation step, and Records and Planning validate their manifests only. The batch write's own preparation stop cannot serve validation: it takes mutation authority, which validation must not take, and store mode refuses it off the store thread.
- **Two plan steps run only in the commit.** Trash metadata and the curation witness add writes to the plan inside the commit. Validation never sees those writes.
- **The store path copies the hooks by hand.** The store-mode batch path repeats the file path's pre-commit sequence. A new hook has to be added twice.
- **Names are resolved per surface.** Folder respelling has 7 separate callers, and the episode store and the import run do not call it. The import run resolves domains per rule. Folders split by case reached a live vault, and on 2026-11-27 a whole-vault export failed with `CASE_COLLISION`.

The owner decided on 2026-11-27 that every surface writes through one validate-then-commit pipeline, with vocabulary resolution, the folder-name reconcile and sensitivity governance hooked in once.

## What Changes

- **Validation and commit share one plan function.** The part of the batch write that needs no lock moves into one function. It completes the plan, including log, navigation index, trash metadata and curation witness writes, and runs the access check on every planned write. Validation calls it and stays read-only. Commit calls it under its lock. Both storage modes use it, and the hand copy in the store path is deleted.
- **Validation binds the caller's page bytes.** The other planned writes are planned again and checked again at commit, so a concurrent write never makes a commit stale through them.
- **Names are resolved once.** One names function runs vocabulary resolution and folder respelling before each surface plans its write. The folder step reuses the look-alike rules from `reconcile-folder-names-at-write`. The batch write refuses a new folder that the function would still steer, and never rewrites it. The access check in the plan function is the one place where sensitivity governance hooks in; this change adds no classifier.
- **Every surface joins.** The note tools, `save_source`, `save_evidence`, Records, Planning, the episode store and device intake validate through the plan function, and the import run calls the names function. Each new refusal is listed per surface in the design.
- **One parity test.** For each write command, the test records the commit's plan, denies each planned path in turn and asserts the same refusal at validation and at commit.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `transactional-vault-writes`: validation and commit share one plan function on every write surface, validation stays read-only, and names are resolved once, before the draft is hashed.

## Impact

- **Code.**
  - The plan function and its steps: `vault.py` (the batch write, the access check and folder respelling), `vault_store/batch.py`, `trash_metadata.py`, `curation_witness.py`.
  - Write leaves that move to the plan function: `records.py`, `planning.py`, `add.py`, `preserve.py`, `note.py`, `edit.py`, `replace.py`, `link.py`, `set_frontmatter_field.py`, `semantic_writes.py`, `episode_store.py`, `adoption_run.py`, `source_imports.py`, `server_transfer.py`.
- **Depends on.** Nothing. It applies in file mode and in store mode.
- **Works with.** The planned `reconcile-folder-names-at-write` extends the folder step. Sensitivity governance hooks into the plan function's access check.
- **Users.**
  - A write that commit refuses today is refused at validation instead, with the same code.
  - Validation stays read-only: it never waits for a writer.
  - The episode store, device intake and the import run gain the folder handling that tool writes already have. The design lists what that changes.
- **Not in this change.** Making the store the default and then the only canonical mode is a separate decision in `make-vault-store-canonical` (D15), proposed in the same pull request.
