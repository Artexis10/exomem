## Why

Exomem writes owner data through many surfaces: note tools, `save_source`, `save_evidence`, `save_episode`, Records and Planning, device uploads, imports and maintenance jobs. Each validates its input in its own way. Checked against main at 26ba98b9a:

- **Validation can pass what commit refuses.** Note validation returns before it plans the log, navigation index and auxiliary writes. The commit then checks every planned write, so a caller can pass validation and get `WRITE_REFUSED` at commit. Planning had the same defect for log-rotation archives (fixed in ca48e5798).
- **One mechanism already gives parity, but not every surface uses it.** Native preparation runs a write through its commit path, including both access checks, and stops before anything is written. Only curation uses it, for `add`, `preserve`, Records and Planning. No tool validates through it: the note tools validate in their own leaf, and `save_source`, `save_evidence`, the episode store, device intake and the import adoption run have no validation step.
- **Two plan steps run after the stop.** Trash metadata and the curation witness add writes to the plan only inside the commit, after the preparation stop. Validation never sees those writes.
- **The store path copies the hooks by hand.** The store-mode batch path repeats the file path's pre-commit sequence. A new hook has to be added twice.
- **Names are resolved per surface.** Folder respelling has 7 separate callers. The episode store and the import adoption run resolve neither vocabulary terms nor folder names. Folders split by case reached a live vault, and on 2026-11-27 a whole-vault export failed with `CASE_COLLISION`.

The owner decided on 2026-11-27 that every surface writes through one validate-then-commit pipeline, with vocabulary resolution, the folder-name reconcile and sensitivity governance hooked in once.

## What Changes

- **Validation runs the commit path to its preparation stop.** Each write leaf validates by running native preparation, so validation refuses exactly what commit refuses.
- **The plan is complete before the stop.** Trash metadata and the curation witness run before the preparation stop, from one function that both storage modes call. The hand copy in the store path is deleted. The heat registration stays in the commit, because it only marks pages as in flight.
- **Validation binds the caller's page bytes.** Log and navigation index writes are planned again and checked again at commit, so a concurrent write never makes a commit stale through them.
- **Names are resolved once.** One names function runs vocabulary resolution and folder respelling before each surface plans its write. `reconcile-folder-names-at-write` extends its folder step. The batch write refuses a new folder that the function would still steer, and never rewrites it. The access check at preparation is the one place where sensitivity governance hooks in; this change adds no classifier.
- **Every surface joins.** The note tools, `save_source`, `save_evidence`, Records, Planning, the episode store, device intake and the import adoption run validate through native preparation. Each new refusal is listed per surface in the design.
- **One parity test.** For each write command, the test records the commit's plan, denies each planned path in turn and asserts the same refusal at validation and at commit.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `transactional-vault-writes`: validation runs the commit path to its preparation stop on every write surface, and names are resolved once, before the draft is hashed.

## Impact

- **Code.**
  - Preparation and the plan steps: `vault.py` (the batch write, the access check and folder respelling), `vault_store/batch.py`, `trash_metadata.py`, `curation_witness.py`.
  - Write leaves that move to native preparation: `records.py`, `planning.py`, `add.py`, `preserve.py`, `note.py`, `edit.py`, `replace.py`, `link.py`, `set_frontmatter_field.py`, `semantic_writes.py`, `episode_store.py`, `adoption_run.py`, `source_imports.py`, `server_transfer.py`.
- **Depends on.** Nothing. It applies in file mode and in store mode.
- **Works with.** `reconcile-folder-names-at-write` extends the folder step. Sensitivity governance hooks into the preparation access check.
- **Users.**
  - A write that commit refuses today is refused at validation instead, with the same code.
  - The episode store, device intake and imports gain the vocabulary and folder handling that tool writes already have. The design lists what that refuses.
- **Not in this change.** Making the store the default and then the only canonical mode is a separate decision in `make-vault-store-canonical` (D15), proposed in the same pull request.
