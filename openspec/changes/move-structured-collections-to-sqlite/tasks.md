## 0. Design and rulings

- [x] 0.1 `proposal.md`, `design.md`, spec deltas (`structured-collections`, `planning`, `records`, `human-owned-structured-files`, `governance-kernel`, `machine-local-state-placement`, `hosted-vault-portability`, `context-roles`), `tasks.md`; including the one-mechanism addendum (`design.md` §14) and the #1457 cost-list answers (§15).
- [x] 0.2 Storage-engine spike with invented data: `benchmarks/collections_sqlite_spike/`.
- [x] 0.3 Orchestrator rulings R1–R10, the #1452 sequencing, and "two writers without the lease are unsupported" folded into `design.md` ("Rulings") and the deltas.
- [ ] 0.4 When #1452's `add-records-bulk-upsert` is archived into the canonical `records` spec, add a `records` MODIFIED delta here for its bulk requirements: store mode drops `BULK_UPSERT_AUDIT_DEPTH`, raises the cap to 500, and makes a batch one transition (`design.md` §9 "Sequencing"). Re-run `openspec validate --all --strict`.

## How the phases ship

Each phase is one or more independently reviewable PRs that leave `main` releasable.

- **Store mode stays dark.** It is off per vault, and file mode is untouched, until the GA gate at the end of P3. Until then a vault can be migrated only with `EXOMEM_COLLECTION_STORE_PREVIEW=1` on a disposable copy.
- **Order.** P4 and P5 may start once P1a is merged, and may merge before or after GA. P6 starts two minor releases after GA (R7).
- **Prerequisites.** #1457 (the interim file fixes) and #1452 (bulk upsert on files, with its per-call cap) land before P1 and are not part of this change.

**Common gates for every phase PR.** Run these before each push, with numbers recorded in the PR body:
- `uv run python scripts/validate-public-artifacts.py --repository`;
- `uvx ruff check --select F src tests`;
- `npm exec --yes @fission-ai/openspec@1.10.0 -- validate --all --strict` when anything under `openspec/` changed;
- `uv run python scripts/generate-capabilities.py --check`;
- scoped pytest for every touched module: `CUDA_VISIBLE_DEVICES= XDG_STATE_HOME=$(mktemp -d) uv run pytest -q -p no:cacheprovider <files>`.

Every phase also re-runs the existing Records and Planning test modules in file mode (`tests/test_record*.py`, `tests/test_records*.py`, `tests/test_planning*.py`, `tests/test_plan_*.py`, `tests/test_structured_*.py`, `tests/test_record_write_scaling.py`), unchanged and green. Behaviour changes are red-first: the failing test is committed or shown before the fix.

## P1a. Store core, built-in writer, query and governance (dark)

- [ ] P1a.1 Red: schema contract tests.
  - STRICT tables.
  - Append-only triggers abort UPDATE and DELETE on `txns`, `audit_effects`, `item_versions`, `item_sources`, `collection_manifests` and `collection_type_versions`.
  - The natural-key partial unique index and `view_path` uniqueness.
  - Readiness refuses SQLite < 3.38 and a state root where WAL does not take effect.
- [ ] P1a.2 `collection_store/schema.py` (DDL, `store_meta.schema_version` 1, forward migrations) and `collection_store/connection.py` (pragmas, one writer connection under the lease, read connections).
- [ ] P1a.3 Placement: the `external-canonical` class in `reserved_paths` / `state_paths` and the placement inventory test. Red first: index rebuild and state migration never touch the store.
- [ ] P1a.4 Type registry with built-ins only: `collection_types` / `collection_type_versions`, package-data `records.yaml` and `planning.yaml` with wire maps, and the named-validator registry (`planning.hierarchy.v1`). `semantic_profile` manifests resolve to the built-in types. Declared-type authoring is P4.
- [ ] P1a.5 Red: the generic writer contract on the store for both built-ins.
  - Append, replay, identity conflict, a natural-key race between two writers, and update with stale container and item guards.
  - Planning add, update and triage with hierarchy validation; create; revise; held, resume and discard.
  - Receipts pass `mutation_terminal.valid_record_receipt`. Golden wire tests show `record_memory` and `plan_memory` arguments, receipts, codes and inspect shapes byte-equal between file and store mode.
- [ ] P1a.6 Guards: generation and row-version tokens (§3), 64-hex and domain-separated. `payload_hash` golden vectors match today's `_payload_hash`.
- [ ] P1a.7 Transactions: one `BEGIN IMMEDIATE` per mutation. `txns` is inserted last with its hash chain, together with `audit_effects`, `item_versions`, `item_sources` and pending `projection_state` in the same transaction. No `log.md` write in store mode.
- [ ] P1a.8 Request identity: `txns.request_id`, `request_hash` and `receipt_json`. Red first: a commit followed by a lost ledger write, retried, gives one transition; the same identity with a different request refuses.
- [ ] P1a.9 Governance precommit inside the transaction (red first: a refusal rolls back and the governance receipt claims no commit). Policy is resolved once per operation, reusing #1457.
- [ ] P1a.10 Red: row-level governance.
  - Policy by `ref`, by `tags`, and by a path below the collection root withholds exactly those rows.
  - Counts, aggregates, pagination, continuation and hierarchy equal the rows-absent case.
  - A hidden-only change keeps a released continuation.
  - Mixed-release mutation refuses.

  Then implement the identity-only authorization pass, the uniform-release decision, and the compiled predicate for path, ref and tag selectors.
- [ ] P1a.11 Parity corpus generator (invented data, every operator and aggregate, both built-ins, saved views, child expansion, hierarchy modes). The parity test requires equal rows, order, totals, aggregates and rendered output between file and store. SQL push-down is allowed only for operators with a passing parity test.
- [ ] P1a.12 Structured readers read the store: `plan_progress`, `due_state` (`_unfiltered_snapshot` and the write deltas), `audit` outcome bindings, `working_set_state` and `working_set_index` candidates, `records_disposition`, and inventory.
- [ ] P1a.13 `bulk_upsert` on the store, adopting #1452's API unchanged: one transaction and one transition with N effects, no `BULK_UPSERT_AUDIT_DEPTH`, cap 500 in store mode while file mode keeps #1452's cap. Re-target #1452's tests to run in both modes.
- [ ] P1a.14 Collection resolution from the `collections` row, plus a contract cache keyed by `(collection_id, manifest_version)`. Index fan-out moves to the derived drain.
- [ ] P1a.15 **Phase gate.**
  - Extend `scripts/measure-records-append-latency.py` (#1457) to store mode with per-stage timers for the §12 budget table.
  - Guarded append p95 < 20 ms at 1,000 and 10,000 items through the real dispatcher, with no stage over its budget.
  - Bulk 500 < 1 s. Guard refresh through `inspect` p95 < 15 ms.
  - Parity test green. Numbers recorded in `design.md` §12 and the PR.

## P1b. Migration, snapshots and hosted (dark)

- [ ] P1b.1 Red: round-trip proof checks (a)–(f) on generated legacy vaults. The vaults cover items and log layouts, both built-ins, legacy chains with `ok`, `gap` and `acknowledged_gap`, held files, filename recipes and managed presentation. A failed check leaves the vault file-canonical and names the collection and the check. Import rewrites no vault file.
- [ ] P1b.2 `collection_store/legacy.py` (read-only extraction of the legacy event parser and chain inspector), the importer, and the preflight `maintain_memory(mode="collections-store", dry_run=true)`.
- [ ] P1b.3 The declared offline migration `collections-store-v1` in the managed upgrade path: standby pre-import with basis, re-import of changed collections at handoff, atomic publish, mode switch. Red first: a write during pre-import is re-imported. It is declared active only at the GA gate (P3.6).
- [ ] P1b.4 Reverse export `maintain_memory(mode="collections-store-export")`: preview-first, atomic per collection, legacy-valid with a checkpoint event. Round-trip test: files → store → files is byte-equal for collections not written in store mode, and legacy-valid (`acknowledged_gap`) for written ones.
- [ ] P1b.5 Red, then the snapshot primitive and replica publisher:
  - a snapshot taken while writers run opens and passes `integrity_check`;
  - the replica is never opened read-write;
  - synchronous flush on quiesce, lease release, shutdown, handoff and export;
  - WAL checkpoints;
  - `exomem collections backup --to/--stdout`.
- [ ] P1b.6 Red, then implement:
  - lease takeover adopts a newer replica, and a service on a copied vault adopts the replica and is writable;
  - divergence refuses collection writes with `COLLECTION_STORE_DIVERGED` while reads and knowledge writes continue;
  - `maintain_memory(mode="collections-store-reconcile")` is preview-first and turns every item changed after the fork point into a held correction;
  - `describe` and the doctor probe state that writers without the lease are unsupported.
- [ ] P1b.7 Hosted: the live store in the cell state root; portability export and staged restore include the snapshot and exclude `-wal` / `-shm` (`hosted_portability`, `hosted_restore`); `cloud_import` of a file vault runs the importer and proof in staging.
- [ ] P1b.8 **Phase gate.**
  - The round-trip proof is green on the generated legacy vaults.
  - Backup under concurrent writes is integrity-checked at 10,000 items.
  - The hosted portability tests pass for export, restore and import.
  - The reverse-export round trip is green.

## P2. Rendered views and edit-back

- [ ] P2.1 Red: synchronous item, manifest and held views.
  - A crash before commit leaves no view ahead of the store.
  - A crash after commit re-renders on restart.
  - `get_page` right after an acknowledged append returns the new values.
  - A simulated Windows rename failure keeps the mutation committed with a `projection_pending` warning.
  - No audit markers or heads are rendered, and values and body are exact.
- [ ] P2.2 View publication: target-adjacent staging and fsync in the transaction, rename after commit and before the acknowledgement, `projection_state` bookkeeping, restart drain, and bounded crash recovery of staging files.
- [ ] P2.3 The asynchronous projector for log-layout views (with the stored log frame, re-emitted byte-identically) and held views. It uses the same pending protocol, with bounded lag, drained on quiesce.
- [ ] P2.4 Red: file tools (`delete_file`, `move_file`, `delete_directory`, `recover_from_trash`) refuse view paths with `COLLECTION_VIEW_PATH`.
- [ ] P2.5 Red, one test per classification row in `design.md` §6:
  - own write ignored; formatting-only edit re-rendered; managed-block-only edit not adopted;
  - a valid edit becomes one governed update with a view-edit transition; a replay of the same bytes gives one transition;
  - a stale base is held `VIEW_CONFLICT`; an invalid edit is held `VIEW_INVALID`; a Planning lifecycle or hierarchy violation is held;
  - a delete is held `VIEW_DELETED` and re-rendered; a move re-renders; an unbound file is held `VIEW_UNBOUND`; a sync-conflict copy is held;
  - a manifest edit becomes revise; a log view is diffed block by block;
  - a typing storm leaves no held correction.
- [ ] P2.6 `file_watcher` hook for paths `projection_state` owns: lease holder only, settle windows (2 s apply, 10 s hold), the same leaf functions as MCP, REST and CLI.
- [ ] P2.7 Held view corrections in `inspect` (the Records `projection` summary and the Planning diagnostics codes with the exact Planning key set unchanged) and in the attention queue.
- [ ] P2.8 **Phase gate.**
  - The edit-back matrix is green.
  - The sync-view append stays within the P1a.15 budget: store and view ≤ 4 ms p95, end to end < 20 ms.
  - Projection lag for aggregate views is reported and bounded under the harness.

## P3. Audit, history, and the GA gate

- [ ] P3.1 Red: `inspect` audit over the table.
  - Statuses: `baseline` / `ok` for native history, imported `gap` / `acknowledged_gap` preserved, and `history_incomplete` from governance.
  - `audit_reader_version` is reported 1 or 2 as today, and interruption never yields a gap.
- [ ] P3.2 `include_agent_history`, the hash-chain verification from `audit_head`, and `rebaseline` for imported legacy gaps only (it refuses on a store-native collection).
- [ ] P3.3 Red: the history page and per-year pages.
  - Newest first, bounded to 200 entries, content-free.
  - Collection-level release filtering: a row-restricted effect is omitted.
  - Edits are ignored and the page re-rendered.
  - Only the current year page is rewritten.
- [ ] P3.4 Red: a store-mode mutation neither reads nor rewrites `Knowledge Base/log.md` (R4), and existing `log.md` history is untouched.
- [ ] P3.5 Documentation: `docs/records.md` (including the stale "exactly five actions") and the Planning docs cover the store, views, edit-back, history pages, backup and the unsupported two-writer setup. Scaffold guidance stays generic (`tests/test_scaffold_no_leak.py`).
- [ ] P3.6 **GA gate.** This turns store mode on for migrated vaults. It requires:
  - P1a, P1b, P2 and P3 merged;
  - `records-release-acceptance`: installed-wheel proof and disposable live MCP evidence for both built-ins in store mode;
  - frozen hosted candidates and `hosted_legacy_profile_schemas.json` byte-identical, and `minimum_records_reader_version` still 2;
  - a dry-run migration of a disposable copy of a real-sized vault with every proof check green.

  Then `collections-store-v1` is declared in the release's upgrade manifest, and the preview flag is retired.

## P4. Declared collection types

- [ ] P4.1 Red: declaration validation, with closed findings per §14.1.
  - `BUILTIN_COLLECTION_TYPE`.
  - `COLLECTION_TYPE_PLACEMENT_OCCUPIED` names the folder, not its files, and is identical whatever the caller can read (R10).
  - A contract test that fails on `semantic_profile ==` or profile-name literals outside the facades and `legacy.py`.
- [ ] P4.2 `schema_memory(subject="collection-types")`: `inventory`, `inspect`, `validate`, `diff` (change classes plus impact preview), `save-collection-type` (with `scaffold`), `history` and `restore`. `infer` is refused. Egress action classes; owner-only `release-widening`; the read-only type view with held type proposals. Red first:
  - a compatible save touches no item;
  - a migrating save is atomic or names the failing item;
  - a stale hash refuses;
  - a kind change is refused.
- [ ] P4.3 Kind semantics: effect labels, served-version defaults and history wording. Red first: a procedural revision versus an observed correction on identical storage.
- [ ] P4.4 Pinned version links (`pin: version`): write validation, resolution from `item_versions`, projection as the item, and the query `.item` / `.version` parts. Red first: the §14.8 grouping by revision, and a withheld pinned target reading as absent.
- [ ] P4.5 Compiler:
  - the generic `collections` lane and `collection_kinds` on roles;
  - `item` anchors from the type registry, type cues and caps;
  - `collection_types_hash` in `generation`;
  - a shipped `context-roles.yaml` revision that keeps the `records` / `planning` lane aliases.

  Red first: the two §14.8 turns serve the current recipe revision and the newest executions. The `context-activation` "Bounded role lanes" wording is reconciled in a delta.
- [ ] P4.6 Governance: subject-level default-deny from `default_audience: owner`, the explain source, and the type-registry hash in the compile fingerprint. Red first, per the `governance-kernel` scenarios.
- [ ] P4.7 The registry replaces the hard-coded layers:
  - `recall_policy`;
  - `hosted_gateway`, plus `test_target_constrained_mutations_are_actually_constrained`;
  - `structured_collections._require_profile_layer`;
  - bootstrap `semantic_profiles`.
- [ ] P4.8 **Phase gate.** An end-to-end journey with invented data:
  1. declare `recipes` in conversation;
  2. add a recipe, then revise it by a view edit;
  3. log executions pinning revisions, and query by revision;
  4. activate both turns;
  5. migrate the type (compatible, then migrating);
  6. show another audience cannot see the recipes until an authored rule names it.

## P5. `record_memory` surface for declared types

- [ ] P5.1 Red: `record_memory` serves declared-type collections.
  - Generic item naming, `_collection_receipt`, generic codes.
  - `action: "transition"` reuses `item_key`, `expected_item_version`, `why` and `changes`, and is checked against the state machine, constraints and validators.
  - `describe(collection_type=…)` teaches the type from its declaration.
  - Planning collections are refused, and `plan_memory` refuses declared types.
- [ ] P5.2 Minimal surface (R8): exactly one new `action` enum value and one optional `collection_type` parameter with a one-line description, and at most one added sentence in the tool description. No other parameter.
- [ ] P5.3 **Phase gate.**
  - A schema-bytes test: the generated local `record_memory` schema grows by ≤ 400 bytes, and `plan_memory` and `schema_memory` by only the `collection-types` subject text.
  - Regenerate the tool schemas, plugin tree, hosted render, v5 candidate and `docs/capabilities.md` per `CONTRIBUTING.md`. Frozen hosted candidates are byte-identical.

## P6. Legacy window close (two minor releases after GA, R7)

- [ ] P6.1 Confirm that no supported vault remains in file mode: every migrated vault is store-canonical, and the doctor probe reports any file-mode vault with the migration remediation.
- [ ] P6.2 Delete every item in `design.md` §13: file mode and its adapters as canonical readers, the #1457 stat-generation cache for collections, and the hard-coded profiles and branches. Measure and report the deleted line count. `legacy.py` and the reverse exporter stay until the owner retires the exporter in a later change.
- [ ] P6.3 Rewrite the tests that pinned file-canonical behaviour (audit markers, manifest heads, `log.md` events, container inventory hashing, rebaseline of out-of-band edits) to the store contract. No test is skipped or deleted without its replacement.
- [ ] P6.4 **Phase gate.** Common gates, the full Records, Planning and types test modules green in store mode, the P1a.15 and P2.8 performance gates re-run, and `openspec validate --all --strict`.
- [ ] P6.5 After merge: synchronize this change's deltas into the canonical specs and archive it with `openspec archive`, validating before and after.
