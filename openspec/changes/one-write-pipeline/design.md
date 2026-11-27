## Context

The owner decided on 2026-11-27 that every write surface goes through one validate-then-commit pipeline. Vocabulary resolution, the folder-name reconcile and sensitivity governance hook in once.

Most of the mechanism exists. `vault.batch_atomic_write` takes a native preparation callback. With it, the write runs its commit path, including both calls of the access check, and stops at `_NativePrepared` before any canonical effect. The store-mode branch stops at the same point (`vault_store/batch.py`, `_stop_at_preparation`). Only curation uses it today: it prepares `add`, `preserve`, Records and Planning this way. Their own tools validate without it, and `save_source` and `save_evidence` have no validation step. `planning.prepare` describes it as running "the same Planning facade through its final native rollback-only boundary".

The gaps, checked against main at 26ba98b9a:

- The note tools validate in their own leaf and return before they plan the log, navigation index and auxiliary writes.
- The episode store, device intake (`server_transfer`) and the import adoption run have no validation step.
- Trash metadata (`prepare_binding_changes`) and the curation witness (`augment_batch`) add writes to the plan after the preparation stop, in both branches. The store branch repeats the file branch's pre-commit sequence by hand.
- Folder respelling (`respell_new_path`) has 7 callers. Vocabulary resolution (`vocabulary_resolution.py`, the domain and entity-type families) runs inside each leaf that uses it.

Primary source: Anthropic, "Writing effective tools for agents — with agents" (2025-09-11). A tool's error has to be actionable when the agent makes the call. A refusal that appears only at commit, after a validation that passed, breaks that rule.

## Goals / Non-Goals

**Goals:**

- Every write surface refuses at validation what its commit refuses, with the same code.
- Vocabulary terms and folder names are resolved in one place for every surface, before the draft is hashed.
- One function holds the plan steps for both storage modes.

**Non-Goals:**

- New rules. The checks stay as they are. New refusals come only from surfaces that gain checks they lacked; D5 lists them.
- A sensitivity classifier. Sensitivity governance hooks into the access check at preparation. Building its rules is its own change.
- The write intake queue. It already refuses before queueing what the arguments alone condemn (`commands.refusal_before_queueing`). It deliberately reads no page, so a save that depends on an earlier queued save is not refused for what that save has not written yet. Full-plan validation there would break that.
- Outside edits and the storage mode. `make-vault-store-canonical` owns both.

## Decisions

### D1. Validation runs the commit path to its preparation stop

Each write leaf validates by running its write with native preparation. The leaf builds its full plan, the access check runs on every planned write, and the write stops before any effect. Validation and commit run the same code, so their refusals agree by construction.

The note tools (`note.py`, `edit.py`, `replace.py`, `link.py`, `set_frontmatter_field.py`, `semantic_writes.py`) replace their early return with this. Seventeen files declare `validate_only` and 30 use it, the dispatcher modules included; each leaf that writes owner pages moves.

Why not a second check function: native preparation already runs the commit's checks. A new function would be a second copy of the same rules.

### D2. The plan is complete before the stop

Trash metadata and the curation witness move ahead of the preparation stop. One function runs the plan steps, and both storage-mode branches call it; the hand copy in `vault_store/batch.py` is deleted.

The heat registration (`working_set_heat.begin_commit`) stays in the commit. It marks pages as in flight and never fails a commit. Running it at validation would leave pages marked in flight that no commit follows.

### D3. Validation binds the caller's page bytes

Validation binds the bytes of the pages that the caller writes, as the review handshake already requires (`existing-edit-review-handshake`). The commit plans the log and navigation index writes again from the current state and checks them again. Nearly every write rewrites `log.md` and a navigation index, so binding those bytes would make any concurrent write turn a commit stale.

### D4. Names are resolved once, before the leaf plans

Native preparation stops inside the batch write, after the leaf has chosen its paths and hashed its draft. So names cannot be resolved there without rewriting bound bytes. Instead, one names function runs before the leaf plans, and every surface calls it:

1. Vocabulary resolution (`vocabulary_resolution`), for the surfaces that name a domain or an entity type.
2. Folder respelling (`respell_new_path`), which steers a new path to an existing sibling folder that differs only by case. `reconcile-folder-names-at-write` extends this step to look-alike names.

The batch write never rewrites a path or a byte. It refuses a planned new folder that the names function would still steer, and its error names the registered folder. The check passes the path that a planned move vacates, as `respell_new_path` takes it today, so a move that changes only a folder's case, such as a case-split repair, still commits. That refusal catches a surface that skipped the names function. The access check at preparation (`connector_boundary.require_write`) is the hook point for sensitivity governance.

### D5. The remaining surfaces join, with their new refusals listed

| Surface | Today | After |
|---|---|---|
| Note tools | Validate in the leaf; log, index and auxiliary writes refused only at commit | The same refusals arrive at validation |
| `save_episode` | No validation; no vocabulary or folder step | Native preparation; access refusals arrive before any write; episodes name no domain or entity type, so vocabulary resolution adds no refusal |
| Device intake (uploads, upload sessions) | No validation; its own egress gate | Native preparation; access refusals arrive before the upload commits; a target folder that differs from an existing one only by case is steered to it |
| Import run (the adoption run and its filing) | No vocabulary or folder step | A filed page whose domain has nearby registered meanings gets `VOCABULARY_DECISION_REQUIRED`. That item stays in the import's staging area with the refusal, and the rest of the batch continues. Folders are steered as for tool writes |
| `save_source`, `save_evidence` | No validation step; native preparation reached only from curation | Native preparation from the tool; access refusals arrive before any write |
| Records, Planning | Manifest checks; native preparation reached only from curation | Native preparation from the tool; the log-archive and auxiliary refusals arrive at validation |

### D6. One parity test

For each write command, the test runs one write and records the commit's plan. It then denies each planned path in turn through the access policy, and asserts that validation and commit return the same refusal code. The commands come from the tool registry. The three surfaces outside the registry (device intake, the adoption run and the cell import) are named in the test, with a one-line reason.

The failure it catches is the one seen before: a planned write that validation never planned, such as the Planning log archive.

### D7. Validation cost is measured under concurrent writes

Native preparation takes the commit lock in file mode (`_BATCH_COMMIT_LOCK`) and the store writer in store mode (`availability.page_writer`). So validation now waits behind commits, and it renders the log and navigation index writes. The change measures validation and commit time before and after, on a generated vault of about 14,000 pages, under a concurrent write stream, on the same instrument. If validation's p95 grows by more than its commit's p95 today, the batch does not ship until the cost is fixed or the owner accepts it. The open question in `make-vault-store-canonical` on storing `log.md` as rows would remove most of the rendering cost.

## Risks / Trade-offs

- **Import batches stop for vocabulary decisions.** An import with many near-miss domains leaves those items staged. → The refusal carries the candidates, and the agent resolves them as it does for tool writes.
- **Validation is slower.** It now waits for the commit lock and renders the auxiliary writes. → D7 measures it under concurrent writes and blocks the batch on a miss.
- **Many leaves change.** Every leaf that writes owner pages moves. → Each moves to an existing mechanism, and D6 covers them all at once.

## Open Questions

None for the owner. The direction is decided; the rest are engineering choices recorded above.
