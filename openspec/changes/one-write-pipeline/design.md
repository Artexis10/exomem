## Context

The owner decided on 2026-11-27 that every write surface goes through one validate-then-commit pipeline. Vocabulary resolution, the folder-name reconcile and sensitivity governance hook in once.

`vault.batch_atomic_write` builds a write's plan, checks it and commits it. It can also stop at `_NativePrepared` before any canonical effect; the store-mode branch stops at the same point (`vault_store/batch.py`, `_stop_at_preparation`). Only curation uses that stop today, for `add`, `preserve`, Records and Planning. The stop runs under mutation authority: in file mode it holds the commit lock, and in store mode `availability.page_writer` refuses it off the store thread (`OFF_THREAD`).

The gaps, checked against main at 26ba98b9a:

- The note tools validate in their own leaf and return before they plan the log, navigation index and auxiliary writes.
- `save_source`, `save_evidence`, the episode store and device intake (`server_transfer`) have no validation step. Records and Planning validate their manifests only.
- Trash metadata (`prepare_binding_changes`) and the curation witness (`augment_batch`) add writes to the plan only inside the commit, in both branches. The store branch repeats the file branch's pre-commit sequence by hand.
- Folder respelling (`respell_new_path`) has 7 callers; the episode store and the import run (`adoption_run`) do not call it. The import run already resolves each rule's domain and refuses an unregistered or deprecated domain before it files anything.

Validation must stay read-only. Three canonical requirements say so: "Remember Preview Does Not Take Mutation Authority" (`write-path-resilience`), and "Validate-Only Edit Is Classified Read-Only" and "Validation-Only Replacement Is Read-Only" (`command-surface`). Validate-only calls skip the write intake queue and take the read path.

Primary source: Anthropic, "Writing effective tools for agents — with agents" (2025-09-11). A tool's error has to be actionable when the agent makes the call. A refusal that appears only at commit, after a validation that passed, breaks that rule.

## Goals / Non-Goals

**Goals:**

- Every write surface refuses at validation what its commit refuses for the same input and state, with the same code.
- Validation stays read-only.
- Vocabulary terms and folder names are resolved in one place for every surface, before the draft is hashed.
- One function holds the plan steps for both storage modes.

**Non-Goals:**

- New rules. The checks stay as they are. New refusals come only from surfaces that gain checks they lacked; D5 lists them.
- A sensitivity classifier. Sensitivity governance hooks into the access check. Building its rules is its own change.
- The write intake queue. It already refuses before queueing what the arguments alone condemn (`commands.refusal_before_queueing`). It deliberately reads no page, so a save that depends on an earlier queued save is not refused for what that save has not written yet.
- Curation's preparation stop. It stays as it is and calls the shared plan function.
- Outside edits and the storage mode. `make-vault-store-canonical` owns both.

## Decisions

### D1. Validation and commit share one plan function; validation stays read-only

The part of the batch write that needs no lock moves into one plan function. It completes the plan and runs the access check on every planned write:

1. The leaf's own writes.
2. The log, navigation index and auxiliary writes.
3. The trash metadata and curation witness writes.

It reads the current state and writes nothing. It takes neither the mutation boundary, nor the writer lease, nor the store writer.

- **Validation** calls the plan function and returns its result. It stays on the read path and never returns `MUTATION_BUSY` because a writer holds the boundary.
- **Commit** calls the same function under its lock, then runs the checks that only a commit can make.
- **Store mode.** Validation reads the store's availability, a read-only check. It returns the refusal that a commit would get for a diverged store or a pending takeover. `OFF_THREAD` describes where a commit runs, not the input, so validation never reports it.

**Vocabulary registry writes.** Today the access check defers the registry decision to the commit, because the vocabulary authority decides over staged files and holds a reservation for the batch (`vocabulary_gate.begin_batch`). Validation instead asks the authority for a read-only decision over the planned images, which `vocabulary_auxiliaries.planned_images` already computes. The reservation and its grant stay commit-only. A note create that registers a new project key is therefore decided at validation too.

A refusal that depends on the input arrives at validation, because both steps run the same code. A refusal that depends on state that changes between the two calls can still arrive at commit, as it does today. Examples are a concurrent write and a reservation lost to a concurrent registry change.

The note tools (`note.py`, `edit.py`, `replace.py`, `link.py`, `set_frontmatter_field.py`, `semantic_writes.py`) replace their early return with a call to the plan function. Seventeen files declare `validate_only` and 30 use it, the dispatcher modules included. Each leaf that writes owner pages moves.

**Why not validate through the preparation stop.** The stop takes mutation authority, which the three requirements above forbid, and store mode refuses it off the store thread. **Why not a second check function.** The plan function is the commit's own code, moved out of the lock, not a copy.

### D2. One function holds the plan steps

Trash metadata and the curation witness run inside the plan function, which both storage-mode branches call. The hand copy in `vault_store/batch.py` is deleted.

The heat registration (`working_set_heat.begin_commit`) stays in the commit. It marks pages as in flight and never fails a commit, so running it at validation would leave pages marked in flight that no commit follows.

### D3. Validation binds the caller's page bytes

Validation binds the bytes of the pages that the caller writes, as the review handshake already requires (`existing-edit-review-handshake`). The commit plans the log, navigation index, trash metadata and curation witness writes again from the current state, and checks them again. Nearly every write rewrites `log.md` and a navigation index. Binding those bytes would let any concurrent write make a commit stale.

### D4. Names are resolved once, before the leaf plans

The plan function runs after the leaf has chosen its paths and hashed its draft, so it cannot resolve names without rewriting bound bytes. Instead, one names function runs before the leaf plans, and every surface calls it:

1. Vocabulary resolution (`vocabulary_resolution`), for the surfaces that name a domain or an entity type.
2. Folder respelling (`respell_new_path`), which steers a new path to an existing sibling folder that differs only by case. A planned change, `reconcile-folder-names-at-write`, not yet written, extends this step to look-alike names.

The batch write never rewrites a path or a byte. The plan function refuses a planned new folder that the names function would still steer, and its error names the registered folder. That refusal catches a surface that skipped the names function.

The check passes the path that a planned move vacates, as `respell_new_path` takes it today. So a move that changes only a folder's case, such as a case-split repair, still commits. The access check in the plan function (`connector_boundary.require_write`) is the hook point for sensitivity governance.

### D5. The remaining surfaces join, with their new refusals listed

| Surface | Today | After |
|---|---|---|
| Note tools | Validate in the leaf; log, index and auxiliary writes refused only at commit | The same refusals arrive at validation |
| `save_source`, `save_evidence` | No validation step | Validation through the plan function; access refusals arrive before any write |
| Records, Planning | Manifest checks only | Validation through the plan function; the log-archive and auxiliary refusals arrive at validation |
| `save_episode` | No validation step; no folder step | Validation through the plan function; episodes name no domain or entity type, so vocabulary resolution adds no refusal |
| Device intake (uploads, upload sessions) | No validation step; its own egress gate | Validation through the plan function before the upload commits; a target folder that differs from an existing one only by case is steered to it |
| Import run (the adoption run and its filing) | Domains resolved per rule, with an unregistered or deprecated domain refused before filing; no folder step | Folders are steered as for tool writes; the rule-level domain refusal stays as it is |

### D6. One parity test

For each write command, the test runs one write and records the commit's plan. It then denies each planned path in turn through the access policy, and asserts that validation and commit return the same refusal code. The commands come from the tool registry. Device intake, the one validating surface outside the registry, is named in the test with a one-line reason. The import run is not a parity surface: it files through the note tools, which the test covers.

For a write that touches the vocabulary registry, the test also denies the registry decision and asserts the same code at both steps. The reservation is not denied in the test, because its refusal depends on a concurrent registry change, not on the input.

The failure it catches is the one seen before: a planned write that validation never planned, such as the Planning log archive.

### D7. Cost is measured under concurrent writes

Validation now renders the log and navigation index writes. The commit now runs the plan function in place of its own copy. The change measures three things before and after, on a generated vault of about 14,000 pages, under a concurrent write stream, on the same instrument:

- validation time;
- commit time;
- the store boundary hold (`make-vault-store-canonical` task 2.9, 100 ms).

If validation's p95 grows by more than its commit's p95 today, or the commit's p95 or the boundary hold gets worse, the batch does not ship until the cost is fixed or the owner accepts it. Storing `log.md` as rows, an open question in `make-vault-store-canonical`, would remove most of the rendering cost.

## Risks / Trade-offs

- **Validation is slower.** It now renders the auxiliary writes. → D7 measures it and holds the batch on a miss.
- **State can change between validation and commit.** A concurrent write can still cause a refusal at commit. → This is today's behaviour; the commit revalidates under its lock.
- **Many leaves change.** Every leaf that writes owner pages moves. → Each moves to the same function, and D6 covers them all at once.

## Open Questions

None for the owner. The direction is decided; the rest are engineering choices recorded above.
