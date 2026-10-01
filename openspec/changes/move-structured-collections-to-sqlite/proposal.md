## Why

Records and Planning are structured collections: typed rows, natural keys, provenance, guarded mutation and an audit trail. Today their canonical form is Markdown files, so the substrate reimplements a relational database by hand:

- natural-key uniqueness is an application scan (`_natural_key_twins`), and duplicates can still appear through edits (`_mark_ambiguous`);
- optimistic concurrency is a SHA-256 over the complete recursive file inventory (`_items_container_hash`), checked with per-file path and directory-census guards;
- the audit trail is JSON lines prepended into `Knowledge Base/log.md`, with a per-item marker comment and a manifest `record_audit` head that must all agree, plus a parent-pointer chain walk capped at 2048 events;
- every write re-reads, re-hashes and re-authorizes the whole collection before it can publish one item.

Measured on `main` (PR #1457), one guarded Records append costs a 12.2 s round trip at 1,000 items and grows linearly with the collection. A 24-row import took about 21 minutes. The proposed bulk upsert (PR #1452) then ran into the audit protocol itself: a batch cannot be one transition, because the file protocol is strictly one event per item and its chain depth is capped.

The owner has decided: **knowledge stays Markdown** (Notes, Entities, Sources, Evidence, Episodes). **Structured collections move to SQLite as their single source of truth**: Records and Planning, which share the collection mechanics, plus their audit logs. The concepts and data model do not change: Records versus Planning versus Notes, typed collections, natural keys, provenance, audit, supersession and governance all stay. Only the storage engine changes. Postgres is out, because a server per machine or cell is not justified. SQLite is embedded and per vault.

## What Changes

- **One collection mechanism; types are data.** Records and Planning become two built-in *collection types* of one generic mechanism.
  - A user or agent can declare a new type in conversation through `schema_memory(subject="collection-types")`, without code: kind, fields, natural key, lifecycle, surfacing rule and default audience. Its collections immediately get storage, keys and guards, audit, row-level governance, views with edit-back, and context-compiler surfacing.
  - The **kind** (`observed`, `intended`, `procedural`, `reference`) is the only semantic switch. It selects version semantics (correction, replan, revision, edit) and the compiler roles that serve the items. Everything else is identical code.
  - `record_memory` and `plan_memory` become typed facades over generic operations, with an unchanged wire.
  - Links may pin an item version.
  - The worked example is a procedural `recipes` type paired with an observed "Recipe Executions" Records collection that pins the recipe revision each run followed.
- **One collection store per vault.** It is a SQLite database holding collections, manifests, items, item versions, provenance, held candidates, and an append-only transaction and audit table. It uses WAL, `synchronous=FULL`, `STRICT` tables and append-only triggers. Uniqueness, natural keys and guards become constraints and counters, not file scans.
- **Guards become generations.** Each collection has a generation counter and each row a row version. The wire keeps the same field names (`expected_container_hash`, `expected_item_version`, `snapshot`, `after_container_hash`) and the same 64-hex shape, but the values are derived from generation and row version instead of from file bytes.
- **Exactly-once lives in the store.** The request identity and the recorded receipt commit in the same transaction as the write. Content replay by payload hash stays.
- **Markdown becomes a projection.** Manifests, items, log-layout collections and held candidates keep today's paths and look, so Obsidian, wikilinks and path-scoped governance keep working. Projection is rendered after commit.
- **Edits made in Obsidian come back through the governed write path.** The file watcher turns an edited view into an ordinary governed update, with an audit transition and an idempotent request identity, and then re-renders the view. An ambiguous, schema-breaking or conflicting edit becomes a held correction, never a silent overwrite. There is exactly one source of truth.
- **Audit is a store table with a rendered history page.** Each collection gets a read-only per-collection history page generated from the table, readable like today's `log.md` entries. Records and Planning no longer write audit lines into `Knowledge Base/log.md`. One mutation is one transition with N row effects, so bulk upsert is one transaction and one transition, and the 2048-event depth cap disappears.
- **Row-level governance.** Each row is evaluated as its own governance subject by the existing evaluator: its view path, its stable reference, and its declared tags, type and project. Policy is resolved once per operation and applied in the query layer before any count, sort or reduction. Store-mode content grants bind to individual canonical rows, so a hidden sibling edit cannot invalidate an unchanged row's grant; old grants never authorize new content, and file-mode grants stay unchanged. Withheld stays indistinguishable from absent. A uniform-release fast path covers the common case where policy cannot tell a collection's rows apart.
- **Migration.** Existing file collections are imported with a verifiable round trip: files to rows, rows rendered and parsed back, the legacy audit chain imported event by event. Import changes no vault file, and a legacy exporter reverses it. It runs as a declared offline migration under the managed standby-upgrade handoff, so the live service keeps serving reads and pauses writes only for the ordinary bounded handoff.
- **Backup.** Snapshots are consistent: the SQLite backup API writes to a staging file, which is integrity-checked and atomically renamed. A sync-safe single-file replica in the vault serves restic and vault copies. Hosted cells keep the live store on the tenant volume, and portability export carries the snapshot as canonical data.
- **Performance targets.** Append p95 < 20 ms at 10,000 rows. Bulk 500 rows < 1 s. Query results match the file path, and query latency is no worse. The spike under `benchmarks/collections_sqlite_spike/` measures the storage engine at 0.7 ms append p95 and 20 ms for a 500-row bulk at 10,000 rows.
- **Deletion.** The hand-built container hashing, path and census guards on collection writes, twin scanning and ambiguity marking, manifest audit heads and item audit markers, `log.md` audit writing and parsing, chain reconstruction and depth caps, content-replay correlation, held files as storage, and per-write full-collection re-reads are removed. `design.md` §13 lists them.
- **Safety amendments (critic review, ruled; `design.md` §16):**
  - check-then-swap publishing, so a write never overwrites an unseen edit, plus a watcher-independent reconcile;
  - a stamp in every view and log block, so stale and foreign edits are held, never applied;
  - a coordinator-recorded store head, with a bounded `COLLECTION_STORE_SYNC_PENDING` on real cross-host handoffs only and an explicit `adopt-local`;
  - instance identity and immediate divergence detection;
  - one vault-side mode marker, a fenced migration, a state descriptor and a tombstoned replica on export;
  - an uncapped legacy importer;
  - cached release decisions and no second fsync;
  - reserved `_Collections/` and listed leak surfaces;
  - backups that never copy the live store;
  - `VIEW_MOVED`;
  - a stdio migrate command and a cutover-bounded handoff proof.
- **Deferred to a follow-up change (ruled):** version-pinned links and the compiler-lane rewrite (surfacing declared types through kind-mapped roles).
- **Datasets are unchanged.** The `dataset` strategy (CSV/TSV/JSON) stays file-canonical and query-only. Those files are human-owned sources, not agent-written collections.

**BREAKING (internal contracts, not tool names):** canonical-storage requirements in `structured-collections`, `planning`, `human-owned-structured-files` and `governance-kernel` change. Direct edits to views are adopted or held instead of being reported as audit gaps. Views are normalized on re-render. `log.md` stops receiving Records and Planning audit events. The guard tokens keep their shape but not their derivation, so a caller holding a pre-migration hash gets one stale refusal and refreshes. Released frozen hosted candidates keep their schemas byte-for-byte.

## Capabilities

### New Capabilities

None. The store is a storage change inside existing capabilities.

### Modified Capabilities

- `structured-collections`: one generic mechanism with declared collection types (authoring, versioning, migration, kinds, pinned links); canonical store, projection, edit-back, audit table and history page, snapshots, migration, latency budget; modified storage, mutation, idempotency, audit, manual-edit and rebaseline requirements.
- `planning`: Planning is a built-in type and `plan_memory` its typed facade; canonical storage and audit wording; manual edits become governed updates or held corrections.
- `records`: Records is a built-in type and `record_memory` its typed facade; held candidates live in the store and are rendered as views.
- `human-owned-structured-files`: views materialize after commit; managed presentation and authored body are projections.
- `governance-kernel`: governance granularity follows the row, not the file; Planning mutation still requires the complete authorized state; a type's `owner` default audience is a subject-level default-deny.
- `machine-local-state-placement`: a new `external-canonical` placement class for the live store.
- `hosted-vault-portability`: export carries the collection store snapshot as canonical data.

## Impact

- **Code:** `records.py`, `record_formats.py`, `record_governance.py`, `record_memory.py`, `records_disposition.py`, `structured_collections.py`, `structured_files.py`, `collection_profiles.py`, `planning.py`, `plan_memory.py`, `plan_progress.py`, `due_state.py`, `audit.py` (outcome bindings), `working_set_index.py`, `file_watcher.py` (edit-back hook), `state_paths.py` and `reserved_paths.py` (placement), `service_upgrade.py` (declared migration), `hosted_portability.py` and `hosted_restore.py`. A new `collection_store` package holds the schema, type registry, generic operations, writer, query, projector, edit-back, importer, exporter and snapshot code. Built-in declarations ship as package data. `collection_profiles.py`, `_SUPPORTED_PROFILES` and the `Records`/`Planning` literals in `recall_policy`, `hosted_gateway` and `working_set*` are replaced by the type registry, and `context_roles` / `working_set` gain the generic `collections` lane and `item` anchors.
- **Tools:** `record_memory` and `plan_memory` keep their names, actions and argument sets for their built-in types. `schema_memory` gains the `collection-types` subject. Per ruling R8, `record_memory` also serves declared types, with a new `transition` action on the local surface and the v5 candidate only. `design.md` §8 lists every contract change. Released frozen hosted candidates (the `hosted-alpha-agent-v1` to `v4` profiles pinned by `hosted_legacy_profile_schemas.json`, and the `minimum_records_reader_version: 2` candidate lock in `hosted_plugins.py`) do not change.
- **Dependencies:** none new. This uses the standard-library `sqlite3`, requires SQLite ≥ 3.38 (STRICT tables, built-in JSON), and refuses the store at readiness otherwise.
- **Pure substrate:** no model is involved. Edit-back parses and validates deterministically; it never interprets prose.
- **Default-off and soft-fail:** the store ships dark behind a per-vault switch until a vault is migrated. A vault on the store refuses collection writes, rather than falling back to files, if the store fails readiness. Knowledge writes are unaffected.
