## 0. Design and rulings

- [x] 0.1 `proposal.md`, `design.md`, spec deltas (`structured-collections`, `planning`, `records`, `human-owned-structured-files`, `governance-kernel`, `machine-local-state-placement`, `hosted-vault-portability`), `tasks.md`; including the one-mechanism addendum (`design.md` §14) and the #1457 cost-list answers (§15).
- [x] 0.2 Storage-engine spike with invented data: `benchmarks/collections_sqlite_spike/`.
- [x] 0.3 Orchestrator rulings R1–R10, the #1452 sequencing, and "two writers without the lease are unsupported" folded into `design.md` ("Rulings") and the deltas.
- [ ] 0.4 When #1452's `add-records-bulk-upsert` is archived into the canonical `records` spec, add a `records` MODIFIED delta here for its bulk requirements: store mode drops `BULK_UPSERT_AUDIT_DEPTH`, raises the cap to 500, and makes a batch one transition (`design.md` §9 "Sequencing"). Re-run `openspec validate --all --strict`.
- [x] 0.5 Critic amendments 1–11 folded into `design.md` §16 (A1–A11), the sections it names and the deltas. Pinned version links and the compiler-lane rewrite moved out of this change (A11).
- [x] 0.6 Owner rulings on N1–N4 applied: all confirmed as implemented, and N3's preflight names the exact release (`design.md` "Rulings").

## How the phases ship

Each phase is one or more independently reviewable PRs that leave `main` releasable.

- **General store mode stays dark.** File collections are untouched until proven migration/GA. The separately owner-approved S1 in `add-collection-query-engine` may create only a NEW owner-only summary collection after its own mandatory safety/outcome gate; it does not migrate existing Records or Planning. Other pre-GA migration remains limited to disposable preview copies.
- **S1 performance exception (owner, 2026-10-04).** The 15 ms full-inspect and 20 ms guarded-append targets remain measured optimisation goals, but do not block the first owner-only S1 collection. Deliver required parent foundation dark with independent evidence while leaving unmet P1a.15 items open. All correctness, permissions, integrity, recovery, compatibility, portability and other applicable bounds remain mandatory; no full-phase or GA completion is implied.
- **Follow-up change (A11).** Version-pinned links and the compiler-lane rewrite are a separate follow-up change, opened after P4.
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
- [ ] P1a.2b Red, then schema support for the amendments:
  - `store_meta` `instance_id`, lineage, forks, `commit_seq`, `store_head_hash` and `last_published_replica_sha256` (A3, A4);
  - `txns.commit_seq` / `store_head_hash`, unique and chained, with a fork test using colliding `txn_id` values;
  - `projection_state` published and pending hash and version plus stat identity (A7);
  - held `code` and `held_bytes`;
  - `collections.verified_through_txn` (A6).
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
- [ ] P1a.13b Release-decision cache per `(audience, policy fingerprint, collection)`, with per-row decisions keyed by row version (A7). Red first: a policy change invalidates it, and a row change re-evaluates only that row.
- [ ] P1a.14 Collection resolution from the `collections` row, plus a contract cache keyed by `(collection_id, manifest_version)`. Index fan-out moves to the derived drain.
- [ ] P1a.15 **Phase gate.**
  - This remains the full phase gate. The narrow S1 exception above permits its first owner-only collection without the two 15/20 ms targets or their supporting append-stage timing targets; all remain measured optimisation goals for S1. It does not permit checking this phase complete or waiving bulk/query/resource bounds or any integrity, authorization, recovery, compatibility or portability gate.
  - Extend `scripts/measure-records-append-latency.py` (#1457) to store mode with per-stage timers for the §12 budget table.
  - Guarded append p95 < 20 ms at 1,000 and 10,000 items through the real dispatcher, with no stage over its budget, including a 10,000-row collection with 10% of rows ref-withheld, on Linux and on a Windows/NTFS runner (A7).
  - No second commit or fsync on the acknowledgement path (the pending hash is written in the main transaction).
  - Bulk 500 < 1 s. Guard refresh through `inspect` p95 < 15 ms.
  - Parity test green. Numbers recorded in `design.md` §12 and the PR.

## P1b. Migration, snapshots and hosted (dark)

- [ ] P1b.1 Red: round-trip proof checks (a)–(f) on generated legacy vaults. The vaults include one whose activity logs exceed 8 MB across more than 128 archives and one collection chain deeper than 2048 (A6). They also cover items and log layouts, both built-ins, legacy chains with `ok`, `gap` and `acknowledged_gap`, held files, filename recipes and managed presentation. A failed check leaves the vault file-canonical and names the collection and the check. Import rewrites no vault file.
- [ ] P1b.2 `collection_store/legacy.py`: an uncapped streaming legacy reader filtered per collection with incremental chain verification (A6). Bounded file-mode readers are unchanged. The importer records `published_sha256` as the hash of the exact parsed bytes (A5). Also and the preflight `maintain_memory(mode="collections-store", dry_run=true)`.
- [ ] P1b.3 The declared offline migration `collections-store-v1` in the managed upgrade path.
  - Standby pre-import with basis; unchanged collections carry their proofs forward, and only changed collections are re-imported and re-proved within the ~40 s cutover, or the upgrade is abandoned cleanly (A11).
  - Atomic publish and the vault-side `mode.json` marker under the separate `collections-store-v1` coordinator capability fence and optional state compatibility descriptor (A5). The fence cut invalidates the old lease token atomically; reacquire before marker cutover.
  - On a multi-host vault, preflight explicitly probes `collections-store-v1` and refuses with `COLLECTION_STORE_COORDINATOR_UPGRADE_REQUIRED` until supported, naming the exact release to upgrade to (N3). Ignored extra request fields do not prove support. Red first.
  - Red first: a write during pre-import is re-imported; a handoff delta over budget abandons the upgrade and leaves files canonical; an old release refuses to start on a migrated vault.
- [ ] P1b.3b `exomem collections migrate` for non-managed (stdio MCP) installs: offline, takes the mutation lock, refuses while a service holds the vault, same preflight and proof (A11). It is declared active only at the GA gate (P3.6).
- [ ] P1b.4 Reverse export `maintain_memory(mode="collections-store-export")`: preview-first, atomic per collection, legacy-valid with a checkpoint event. It sets the mode marker to `exported` and tombstones the replica (A5). Red first: no host adopts a tombstoned replica or a stale local store after export. Round-trip test: files → store → files is byte-equal for collections not written in store mode, and legacy-valid (`acknowledged_gap`) for written ones.
- [ ] P1b.5 Red, then the snapshot primitive and replica publisher:
  - a snapshot taken while writers run opens and passes `integrity_check`;
  - the replica is never opened read-write;
  - synchronous flush on quiesce, lease release, shutdown, handoff and export;
  - WAL checkpoints;
  - `exomem collections backup --to/--stdout`, and the existing restic timer hooked to run it first, with the live store excluded entirely (A9);
  - replica publication check-then-swap against `last_published_replica_sha256` (A4), coalesced to at most one publish per 60 s under steady writes (A9, N4).
- [ ] P1b.6 Red, then implement:
  - optional compatibility recognition/enrollment and installed-candidate support remain distinct from physical migration: preserve enrolled IDs through family changes and external adoption, reject unknown IDs, keep fresh file-only manifests unchanged, refuse unsupported/forged candidates before stopping the worker and recheck at cutover. Keep the legacy target triple in both mixed operator/supervisor directions; bind verified support privately to the exact candidate and discard it on failed or mismatched inspection. Restore serving admission before rejected-candidate cleanup outside the stop/migration timeout. A conditionally supported unchanged-family candidate skips migration. The dark foundation advertises no store runtime support and has no enrollment caller until the trusted adapter exists; prove preservation/replay, old-reader refusal and pre-stop admission independently;
  - Explicit new-store identity CAS after export/re-migration: atomic generation advance, former-head clear and lease revocation; same-target lost-ack replay preserves later head reports. Generations never reset; stale replay through intervening identity transitions conflicts. The coordinator does not evaluate migration proofs or change the marker.
  - the separate vault-specific `collections-store-v1` capability fence in the existing coordinator transaction/token machinery, with generation CAS/replay, old-client refusal and stale-token head rejection. Governance 3/4 and file-only manifests remain unchanged. Record the optional compatibility descriptor only for mixed/store vaults and prove old-reader startup refusal separately from writer-lease fencing;
  - a trusted renewer-safe head provider and synchronous ordinary-release flush while writer authority remains valid. Persist the valid holder's head atomically on renew/release and retain it after release/expiry; operator release preserves it. No renewer borrows the opening-thread writer connection;
  - the store-wide head exchange with the coordinator on renew and release (C20). A new holder facing a foreign recorded head refuses with the retryable `COLLECTION_STORE_SYNC_PENDING` until its replica reaches that head, re-checks on replica change and every 10 s, and raises attention after 15 minutes. A store with no foreign head ever recorded never waits (A3);
  - `exomem collections adopt-local` / `maintain_memory(mode="collections-store-adopt-local")`: owner-only, preview-first, records the fork point and lineage, and reconciles the other side's later-arriving delta into held corrections (A3);
  - instance identity and lineage; the `_Collections/` watch plus reconcile poll; any foreign replica or stamp immediately sets `COLLECTION_STORE_DIVERGED` (A4). Red first: a Windows and a WSL service on one vault (two state roots) are detected on the first foreign publish;
  - lease takeover adopts a newer replica, and a service on a copied vault adopts the replica and is writable;
  - divergence refuses collection writes with `COLLECTION_STORE_DIVERGED` while reads and knowledge writes continue;
  - `maintain_memory(mode="collections-store-reconcile")` is preview-first and turns every item changed after the fork point into a held correction;
  - `describe` and the doctor probe state that writers without the lease are unsupported.
- [ ] P1b.7 Hosted: the live store in the cell state root; portability export and staged restore include the snapshot and exclude `-wal` / `-shm` (`hosted_portability`, `hosted_restore`); `cloud_import` of a file vault runs the importer and proof in staging.
- [ ] P1b.8 **Phase gate.**
  - The round-trip proof is green on the generated legacy vaults.
  - Backup under concurrent writes is integrity-checked at 10,000 items.
  - The hosted portability tests pass for export, restore and import.
  - Replica sync churn is reported in bytes per hour at 1, 10 and 60 writes per minute on a 10,000-row store, within the stated budget (A9).
  - Handoff scenarios pass: routine idle-release re-acquisition on one host never waits, a cross-host handoff on a lagging replica waits boundedly, and adopt-local loses nothing.
  - The reverse-export round trip is green.

## P2. Rendered views and edit-back

- [ ] P2.1 Red: synchronous item, manifest and held views.
  - A crash before commit leaves no view ahead of the store.
  - A crash after commit re-renders on restart.
  - `get_page` right after an acknowledged append returns the new values.
  - A simulated Windows rename failure keeps the mutation committed with a `projection_pending` warning.
  - No audit markers or heads are rendered, and values and body are exact.
- [ ] P2.1b Red, check-then-swap (A1):
  - an agent update landing inside the 2 s settle window, with no watcher running, or after an offline edit, holds the human's bytes as `VIEW_CONFLICT` and loses nothing;
  - a file created between the aside rename and the install is not replaced (no-clobber, N2);
  - the startup reconcile classifies offline edits before the projector runs.
- [ ] P2.1c Red, stamps (A2): a stale-buffer or second-device save stamped v4 over v5 is held `VIEW_CONFLICT`; a foreign-instance stamp holds `VIEW_FOREIGN` and sets divergence; a tampered stamp is `VIEW_INVALID`; only exact imported bytes may be unstamped; the same rules apply per log block.
- [ ] P2.2 View publication: target-adjacent staging and fsync in the transaction, pending hash recorded in the main transaction, check-then-swap install after commit and before the acknowledgement, the stamp rendered in every view and log block, the startup and 60 s reconcile with a stat-identity fast path, `projection_state` bookkeeping, restart drain, and bounded crash recovery of staging files.
- [ ] P2.3 The asynchronous projector for log-layout views (with the stored log frame, re-emitted byte-identically) and held views. It uses the same pending protocol, with bounded lag, drained on quiesce.
- [ ] P2.4 Red: file tools (`delete_file`, `move_file`, `delete_directory`, `recover_from_trash`) refuse view paths with `COLLECTION_VIEW_PATH`. The `_Collections/` directory is reserved and denied everywhere. One test per A8 leak surface: listing and counts, inbound links and graph, the resolver, the indexes, refusal shapes, attention and due-state, history links, held views, inventory, and sync-conflict copies.
- [ ] P2.5 Red, one test per classification row in `design.md` §6:
  - own write ignored; formatting-only edit re-rendered; managed-block-only edit not adopted;
  - a valid edit becomes one governed update with a view-edit transition; a replay of the same bytes gives one transition;
  - a stale base is held `VIEW_CONFLICT`; an invalid edit is held `VIEW_INVALID`; a Planning lifecycle or hierarchy violation is held;
  - a delete is held `VIEW_DELETED` and re-rendered; a move within the root is a governed `view_move` that keeps the moved path; a move out of the root is held `VIEW_MOVED` (A10); an unbound file is held `VIEW_UNBOUND`; a sync-conflict copy is held;
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
- [ ] P3.2 `include_agent_history`, the hash-chain verification from `audit_head` with the verified-through watermark (O(new transactions), A6), and `rebaseline` for imported legacy gaps only (it refuses on a store-native collection).
- [ ] P3.3 Red: the history page and numbered pages.
  - Newest first, 200 entries on the main page, content-free, older transitions on numbered pages of 500, and only the newest numbered page rewritten (A6).
  - Rendered for the intersection of audiences: an effect any reader of the page could not see is omitted, and a mixed bulk transaction drops its `why` (A8).
  - Edits are ignored and the page re-rendered.
- [ ] P3.4 Red: a store-mode mutation neither reads nor rewrites `Knowledge Base/log.md` (R4), and existing `log.md` history is untouched.
- [ ] P3.5 Documentation: `docs/records.md` (including the stale "exactly five actions") and the Planning docs cover the store, views, edit-back, history pages, backup and the unsupported two-writer setup. Scaffold guidance stays generic (`tests/test_scaffold_no_leak.py`).
- [ ] P3.6 **GA gate.** This turns store mode on for migrated vaults. It requires:
  - P1a, P1b, P2 and P3 merged;
  - `records-release-acceptance`: installed-wheel proof and disposable live MCP evidence for both built-ins in store mode;
  - frozen hosted candidates and `hosted_legacy_profile_schemas.json` byte-identical, and `minimum_records_reader_version` still 2;
  - a dry-run migration of a disposable copy of a real-sized vault with every proof check green.

  Then `collections-store-v1` is declared in the release's upgrade manifest, and the preview flag is retired.

## P4. Declared collection types

Not in this change (A11; follow-up change): version-pinned links, and the generic `collections` compiler lane with `collection_kinds`, `item` anchors and `collection_types_hash`. Here, `surfacing` is validated and stored only.

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
- [ ] P4.6 Governance: subject-level default-deny from `default_audience: owner`, the explain source, and the type-registry hash in the compile fingerprint. Red first, per the `governance-kernel` scenarios.
- [ ] P4.7 The registry replaces the hard-coded layers:
  - `recall_policy`;
  - `hosted_gateway`, plus `test_target_constrained_mutations_are_actually_constrained`;
  - `structured_collections._require_profile_layer`;
  - bootstrap `semantic_profiles`.
- [ ] P4.8 **Phase gate.** An end-to-end journey with invented data:
  1. declare `recipes` in conversation;
  2. add a recipe, then revise it by a view edit;
  3. log executions (unpinned link), and query them;
  4. migrate the type (compatible, then migrating);
  5. show another audience cannot see the recipes until an authored rule names it.

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
