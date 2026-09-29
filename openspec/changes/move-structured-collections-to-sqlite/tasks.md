## 1. Design and ruling

- [x] 1.1 `proposal.md`, `design.md`, spec deltas (`structured-collections`, `planning`, `records`, `human-owned-structured-files`, `governance-kernel`, `machine-local-state-placement`, `hosted-vault-portability`, `context-roles`), `tasks.md`. Addendum: one generic mechanism with declared collection types (`design.md` §14).
- [x] 1.2 Storage-engine spike with invented data: `benchmarks/collections_sqlite_spike/` (append, bulk, governed query, snapshot, view publish at 1,000 and 10,000 rows).
- [ ] 1.3 Owner rulings R1–R10 (`design.md`, "Needs ruling") applied to the design and deltas.

## 2. Store core

- [ ] 2.1 Red: schema contract tests: STRICT tables; append-only triggers abort UPDATE/DELETE on `txns`, `audit_effects`, `item_versions`, `item_sources`, `collection_manifests`; the natural-key partial unique index; `view_path` uniqueness; readiness refuses SQLite < 3.38 and a non-WAL state root.
- [ ] 2.2 `collection_store/schema.py` (DDL, `store_meta.schema_version` 1, forward migrations) and `collection_store/connection.py` (pragmas, busy timeout, one writer connection under the lease, read connections).
- [ ] 2.3 Placement: `external-canonical` class in `reserved_paths` / `state_paths` and the placement inventory test. Index rebuild and state migration never touch the store (red first).

## 3. Writer on the store (Records and Planning)

- [ ] 3.1 Red: the shared writer contract on the store, covering append, replay, identity conflict, natural-key conflict (race of two writers), update with stale container and item guards, Planning add/update/triage including hierarchy validation, create, revise, rebaseline refusal on a store-native collection, held/resume/discard. Receipts are byte-shape-equal to today's (`mutation_terminal.valid_record_receipt`).
- [ ] 3.2 Generation and row-version guard tokens (`design.md` §3) with domain-separated digests; `payload_hash` derivation unchanged (golden vectors from today's `_payload_hash`).
- [ ] 3.3 Transactions: one `BEGIN IMMEDIATE` per mutation, `txns` inserted last with its hash chain, `audit_effects` per row, `item_versions`, `item_sources`, `projection_state` marked pending in the same transaction.
- [ ] 3.4 Request identity: `txns.request_id` / `request_hash` / `receipt_json`. Red first: a commit followed by a lost transport ledger write, retried with the same identity, gives one transition. The same identity with a different request refuses.
- [ ] 3.5 Governance precommit inside the transaction; refusal rolls back (red first); governance receipts do not claim commit on rollback.
- [ ] 3.6 Manifest cache keyed by `manifest_version` (replaces the nine parses per append).

## 4. Bulk upsert (per ruling R2)

- [ ] 4.1 Re-target #1452's tests to the store: one transaction and one transition with N effects, abort/skip, all-`unchanged` replay writes nothing, no `BULK_UPSERT_AUDIT_DEPTH`.
- [ ] 4.2 `bulk_upsert` action on the store writer; describe text; surface regeneration (§12).

## 5. Query layer and governance

- [ ] 5.1 Red: row-level policy by `ref`, by `tags` and by a path below the projection root withholds exactly those rows. Counts, aggregates, pagination, continuation and hierarchy equal the rows-absent case. A hidden-only change keeps a released continuation valid. A log view with one withheld row is withheld as a file.
- [ ] 5.2 Policy resolved once per operation (reuse #1457 fix 1); identity-only authorization pass; temporary authorized set; uniform-release decision.
- [ ] 5.3 Parity corpus generator (invented data, every operator and aggregate, both profiles, saved views, child expansion, hierarchy modes) and parity test: file path versus store path give equal rows, order, totals, aggregates and rendered output.
- [ ] 5.4 SQL push-down on the uniform-release path only for operators with a passing parity test; per-collection expression indexes for saved-view fields.
- [ ] 5.5 Readers moved to store queries: `plan_progress`, `due_state` (`_unfiltered_snapshot`, write deltas), `audit` outcome bindings, `working_set_index` planning candidates, `records_disposition`, inventory.

## 6. Projection

- [ ] 6.1 Red: a committed mutation marks views pending, and the projector renders them after commit. A crash after commit re-renders on restart. A projection failure does not fail the mutation and is reported. Rendering of values and body is exact. No audit markers or heads are rendered.
- [ ] 6.2 Projector task: target-adjacent staging, fsync, rename, `projection_state` update, view index sync moved off the acknowledgement path.
- [ ] 6.3 History page and per-year pages from `txns` / `audit_effects`: newest first, bounded, content-free, collection-level release filtering (red first: a row-restricted effect is omitted).
- [ ] 6.4 Held candidate and held view-correction views under `Held/` (read-only).

## 7. Edit-back

- [ ] 7.1 Red, per classification row in `design.md` §6: own write ignored; formatting-only edit re-rendered; valid edit becomes one governed update with a view-edit transition; replay of the same bytes gives one transition; stale base is held `VIEW_CONFLICT`; invalid edit is held `VIEW_INVALID`; delete is held `VIEW_DELETED` and re-rendered; unbound file is held `VIEW_UNBOUND`; sync-conflict copy is held; manifest edit becomes revise; log-view block diff; history and held views are read-only; typing storm leaves no held correction.
- [ ] 7.2 `file_watcher` hook for `projection_state`-owned paths, lease-holder only, settle windows (2 s apply, 10 s hold).
- [ ] 7.3 Held view corrections in `inspect` (Records `projection` summary; Planning diagnostics codes) and in the attention queue.

## 8. Migration

- [ ] 8.1 Red: round-trip proof checks (a)–(f) on generated legacy vaults: items and log layouts, both profiles, legacy chains with `ok`, `gap` and `acknowledged_gap`, held files, filename recipes, managed presentation. A failed check leaves the vault file-canonical and names the collection and check. Import rewrites no vault file.
- [ ] 8.2 `collection_store/legacy.py`: read-only extraction of the legacy event parser and chain inspector; importer; preflight `maintain_memory(mode="collections-store", dry_run=true)`.
- [ ] 8.3 Declared offline migration `collections-store-v1` in the managed upgrade path: standby pre-import with basis, re-import of changed collections at handoff, atomic publish, mode switch (red first: a write during pre-import is re-imported).
- [ ] 8.4 Reverse export `maintain_memory(mode="collections-store-export")`: preview-first, atomic per collection, legacy-valid with a checkpoint event. Round-trip test files → store → files.

## 9. Snapshots, replica, hosted

- [ ] 9.1 Red: a snapshot taken while writers run opens and passes `integrity_check`. The replica is never opened read-write. Lease takeover adopts a newer replica. Divergence refuses collection writes while reads and knowledge writes continue.
- [ ] 9.2 Snapshot primitive, coalesced replica publisher, synchronous flush on quiesce, lease release, shutdown, handoff and export; WAL checkpoints; `exomem collections backup --to/--stdout`.
- [ ] 9.3 Hosted: live store in the cell state root; portability export and staged restore include the snapshot and exclude `-wal` / `-shm` (`hosted_portability`, `hosted_restore`); restore surfaces differences as held view corrections.

## 10. Measure

- [ ] 10.1 Extend `scripts/measure-records-append-latency.py` (#1457) to the store: guarded append p95 at 1,000 and 10,000 items; bulk 500; query parity latency at 100, 1,000 and 10,000. Record before and after in `design.md` and the PR. Targets: append p95 < 20 ms, bulk 500 < 1 s, query no worse than the file path.

## 11. Delete the hand-built machinery (after the legacy window, ruling R7)

- [ ] 11.1 Remove every item in `design.md` §13. Measure and report the deleted line count. File mode and its tests go with it; `legacy.py` stays until the reverse exporter is retired.
- [ ] 11.2 Rewrite the tests that pinned file-canonical behaviour (audit markers, manifest heads, `log.md` events, container inventory hashing, rebaseline of out-of-band edits) to the store contract. No test is skipped or deleted without its replacement.

## 12. Surface and documentation

- [ ] 12.1 `record_memory` / `plan_memory` describe text and docstrings; `docs/records.md` (which also still says "exactly five actions") and Planning docs; scaffold guidance stays generic (`tests/test_scaffold_no_leak.py`).
- [ ] 12.2 Regenerate tool schemas, plugin tree, hosted render, v5 candidate and `docs/capabilities.md` per `CONTRIBUTING.md`. Frozen hosted candidates and `hosted_legacy_profile_schemas.json` are byte-identical; `minimum_records_reader_version` stays 2.

## 13. Collection types (one mechanism)

- [ ] 13.1 Red: declaration validation (closed findings per `design.md` §14.1); built-in `records` and `planning` declarations load through the same registry; `BUILTIN_COLLECTION_TYPE`; no mechanism branches on type names. This is a contract test that greps for `semantic_profile ==` and profile-name literals outside the facades and legacy import.
- [ ] 13.2 Type registry in the store (`collection_types`, `collection_type_versions`), package-data built-in declarations with wire maps, named-validator registry (`planning.hierarchy.v1`, `references.acyclic.v1`), and `collection_type:` manifests with `semantic_profile` aliases.
- [ ] 13.3 Generic operations `collections.ops.*`, including `transition` against the declared state machine. `record_memory` and `plan_memory` become facades, with golden wire tests showing receipts, codes and inspect shapes byte-equal before and after.
- [ ] 13.4 `schema_memory(subject="collection-types")`: `inventory`, `inspect`, `validate`, `diff` (change classes and impact preview), `save-collection-type`, `history`, `restore`; `infer` refused; egress action classes; the owner-only `release-widening`. Red first: compatible save touches no item; migrating save is atomic or names the failing item; stale hash refuses; kind change refused.
- [ ] 13.5 Kind semantics: effect labels, served-version defaults, history wording (red first for procedural revision versus observed correction on identical storage).
- [ ] 13.6 Pinned version links (`pin: version`): write validation, resolution from `item_versions`, projection as the item, and query `.item` / `.version` parts. Red first: the Recipes/Executions grouping in `design.md` §14.8, and a withheld pinned target reading as absent.
- [ ] 13.7 Compiler: the generic `collections` lane, `collection_kinds` on roles, `item` anchors from the type registry, type cues and caps, `collection_types_hash` in `generation`. A shipped `context-roles.yaml` revision keeps `records` / `planning` lane aliases. Red first: the two turns in §14.8 serve the current recipe revision and the newest executions respectively.
- [ ] 13.8 Governance: subject-level default-deny from `default_audience: owner`; explain source; type-registry hash in the compile fingerprint. Red first, per the `governance-kernel` scenarios.
- [ ] 13.9 The registry replaces hard-coded layers in `recall_policy`, `hosted_gateway` (plus `test_target_constrained_mutations_are_actually_constrained`), `structured_collections._require_profile_layer`, and bootstrap `semantic_profiles`.
- [ ] 13.10 An end-to-end journey with invented data: declare `recipes` in conversation, add, revise by view edit, log executions pinning revisions, query by version, activate both turns, then migrate the type (compatible, then migrating).

## 14. Deliver

- [ ] 14.1 Gates: privacy gate, `ruff --select F`, `openspec validate --all --strict`, `generate-capabilities.py --check`, scoped pytest per touched module.
- [ ] 14.2 After merge: synchronize the deltas into the canonical specs and archive this change with `openspec archive`.
