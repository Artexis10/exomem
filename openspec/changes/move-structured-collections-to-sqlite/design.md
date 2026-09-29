## Context

Records and Planning share one collection engine. `planning.py` calls `records.create_collection`, `append_record`, `update_record` and `_lifecycle_mutation`. The adapters (`record_formats.MarkdownItemsAdapter`, `MarkdownLogAdapter`), governance (`record_governance`) and manifest code (`structured_collections`) are common to both, and `collection_profiles` switches the vocabulary (`record_audit` or `plan_audit`, `Records audit-v1` or `Planning audit-v1`).

Today the canonical form is Markdown:

- a `_collection.md` manifest;
- either one item file per row with a `# exomem-record-audit:` / `# exomem-plan-audit:` marker comment, or one log file of heading blocks with `<!-- exomem-record-id -->` and `<!-- exomem-record-audit -->` comments;
- JSON audit events prepended into `Knowledge Base/log.md` and rotated into `_archive/logs/`.

Everything relational is hand-built on top of that:

| Concern | Today (file:line on `main`) | Cost |
| --- | --- | --- |
| Uniqueness | `_natural_key_twins` scan (`records.py:3759`); duplicates found on read are flagged by `_mark_ambiguous` (`record_formats.py:2929`) | O(N) per write; duplicates can still exist |
| Optimistic concurrency | `_items_container_hash` over the full recursive inventory (`records.py:4134`, `record_formats.py:561-571`); `PathGuard` / `DirectoryCensusGuard` re-checked in `vault.batch_atomic_write` | O(N) hashing and about 20 directory censuses per append (#1457) |
| Audit | event lines in `log.md` (`_audit_body` `records.py:4009`, `_plan_required_audit` `:3986`); item marker; manifest head (`render_manifest_audit_head` `record_formats.py:1580`); chain walk `_reconstruct_audit_chain` (`records.py:2042-2176`) capped at 2048 | whole `log.md` read and rewritten per write (`vault.py:7243`); every item must be marker-bound |
| Replay | `_payload_hash` plus `_replay_audit_correlation` (`records.py:2394`), which needs a healthy chain | chain walk on replay |
| Governance | per-path `_authorize` for every file (`record_governance.py:936`), with policy re-resolved per call (#1457 hot spot 1) | about 3,200 policy resolutions per append at N=1000 |
| Reads | full adapter read, then `query_data.evaluate_rows` over every row | about 7.4 ms per item per refresh |

PR #1457 measured a guarded append round trip of 12.2 s at N=1000, O(N). PR #1452 found that one bulk transition cannot be expressed in the file audit protocol.

The owner's decisions for this change:

- Knowledge (Notes, Entities, Sources, Evidence, Episodes) stays Markdown.
- Records and Planning, with their audit logs, move to SQLite as the single source of truth.
- The concepts and data model stay; only the storage engine changes.
- Markdown views are projections with governed edit-back (option 2).
- Audit is SQLite-authoritative, with a read-only rendered history page per collection.
- Postgres is out.
- Collections are ONE general mechanism. Records and Planning are built-in types of it. A new type (for example Recipes) is declared in conversation through `schema_memory` without code, and immediately gets storage, keys, guards, audit, governance, views, edit-back and compiler surfacing (§14).

## Goals / Non-Goals

**Goals**
- One embedded store per vault that is the only source of truth for structured collections of every type (Records, Planning and declared types), their manifests, type declarations, held candidates and audit.
- One generic collection mechanism, with types as data: kind, fields, natural key, lifecycle, surfacing and default audience are declared, not coded (§14).
- Relational guarantees from the engine: unique natural keys, atomic multi-row writes, row versions, append-only audit, and exactly-once by request identity.
- Obsidian keeps working: views sit at today's paths and edits made in them come back through the governed write path.
- Unchanged external tool contracts wherever possible, with every change listed (§8). Released frozen hosted candidates stay unchanged.
- Row-level governance with withheld indistinguishable from absent, enforced before any reduction.
- A verifiable, reversible, zero-downtime migration.
- Consistent snapshots for backup, vault copies and hosted portability.
- Append p95 < 20 ms at 10,000 rows; bulk upsert of 500 rows < 1 s; query parity.
- Deleting the hand-built guard, snapshot and audit-marker machinery (§13).

**Non-Goals**
- Moving knowledge notes, sources or evidence into a database.
- Moving the `dataset` strategy. CSV, TSV and JSON datasets stay human-owned, file-canonical and query-only.
- A server database, replication protocol, or multi-writer store. The existing single-writer lease stays the only write authority.
- A delete action or new policy language. New collection *types* are data (§14); new *kinds* change only by shipped revision.
- Changing released frozen hosted candidates.
- Server-side interpretation of edited prose. Edit-back is deterministic parsing and validation only (pure substrate).

## Decisions

### 1. One store per vault, and where it lives

The live store is one SQLite file per vault: `collections.sqlite` under the vault's state root (`state_paths.vault_state_dir`), with the pragmas `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON` and `busy_timeout`. It is classified with a new placement class, `external-canonical`. Like `external-state`, it lives outside the vault so no file-sync agent ever sees a live WAL database. Unlike `external-state`, it is canonical:
- it is never rebuilt from other state and never deleted by index maintenance or rebuild;
- migration moves it and never drops it;
- backups and portability include it.

The vault carries a **replica**: `Knowledge Base/_Collections/collections.sqlite`. It is a single-file (`journal_mode=DELETE`) consistent snapshot, published by the backup API to a target-adjacent staging file, checked with `PRAGMA quick_check`, and atomically renamed into place.
- **When it is published:** after committed transactions, coalesced with a 1 s window, off the acknowledgement path. It is always flushed synchronously on quiesce, writer-lease release, shutdown, upgrade handoff and portability export.
- **What it carries:** `store_id`, a store-wide `commit_seq` and the lease epoch in `store_meta`.
- **How it is used:** it is what restic, vault copies and multi-host replicas carry. It is never opened for writing in place.

Single source of truth is preserved because exactly one live store accepts writes: the one held by the writer-lease holder. The replica is a published copy of it, in the same sense that the Markdown views are.

**Multi-host takeover** (opt-in `multi-host-writer-lease`):
- A host that acquires the lease compares its local live store with the vault replica. If the replica has the same `store_id` and a `commit_seq` ahead of the local store, and the local head transaction appears in the replica, it adopts the replica: copy, `integrity_check`, swap. Then it serves writes.
- If the local store holds transactions that the replica does not, collection writes refuse `COLLECTION_STORE_DIVERGED` and operator attention is raised. Knowledge writes and all reads continue.
- The recovery point for a crashed writer is the coalescing window. That is the same exposure as today's file replication lag. Stranded transactions stay in the crashed host's store for operator recovery.

*Alternative rejected:* the live store inside the vault. The ratified `machine-local-state-placement` requirement exists because sync agents hash, hold and replace database files, and a WAL database copied mid-checkpoint is corrupt. This is **Needs ruling R1**, because the owner's brief says "per vault" without naming the root.

### 2. Schema

These are STRICT tables. `collection_store/schema.py` owns the DDL and migrations, with `store_meta.schema_version` starting at 1. The spike (`benchmarks/collections_sqlite_spike/spike.py`) implements the item, version, source, transaction and effect core of it.

```sql
store_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)
  -- schema_version, store_id (uuid4), commit_seq, created_at,
  -- migrated_from ('files' | NULL), lease_epoch

collections(
  collection_id TEXT PRIMARY KEY,            -- the existing manifest exomem_id
  type_name TEXT NOT NULL REFERENCES collection_types,   -- 'records', 'planning' or a declared type (sec. 14)
  type_version INTEGER NOT NULL,
  manifest_path TEXT NOT NULL UNIQUE,        -- vault-relative projection path of _collection.md
  source_path TEXT NOT NULL UNIQUE,          -- projection root (items) or log file (log layout)
  layout TEXT NOT NULL CHECK (layout IN ('markdown-items','markdown-log')),
  manifest_version INTEGER NOT NULL,         -- FK into collection_manifests
  generation INTEGER NOT NULL,               -- +1 per committed transaction touching the collection
  audit_head TEXT,                           -- event_hash of the latest transaction
  audit_reader_version INTEGER NOT NULL,     -- 1 or 2, carried for the wire (sec. 8)
  legacy_audit_status TEXT,                  -- status imported from files, NULL if native
  log_frame_json TEXT,                       -- log layout only: bytes outside the item section,
                                             -- BOM, newline style, final-newline state (re-emitted exactly)
  created_txn INTEGER NOT NULL, updated_txn INTEGER NOT NULL)

collection_types(name TEXT PRIMARY KEY, current_version INTEGER NOT NULL, builtin INTEGER NOT NULL)
collection_type_versions(name, version, declaration_json TEXT NOT NULL, declaration_hash TEXT NOT NULL,
  change_class TEXT NOT NULL, txn_id INTEGER NOT NULL,
  PRIMARY KEY (name, version))                -- append-only (sec. 14.5)

collection_manifests(                        -- manifest history, append-only
  collection_id, manifest_version, manifest_text TEXT NOT NULL,   -- exact authored text
  manifest_hash TEXT NOT NULL, schema_json TEXT NOT NULL,         -- parsed contract
  natural_key_json TEXT, txn_id INTEGER NOT NULL,
  PRIMARY KEY (collection_id, manifest_version))

items(
  row_id INTEGER PRIMARY KEY,
  collection_id TEXT NOT NULL REFERENCES collections,
  item_key TEXT NOT NULL,                    -- record_id / plan_id (uuid), unchanged derivation
  natural_key TEXT,                          -- structured_collections serialization, NULL if incomplete
  row_version INTEGER NOT NULL,
  schema_version INTEGER NOT NULL,
  values_json TEXT NOT NULL,                 -- canonical JSON of declared fields (sorted, NFC)
  body TEXT NOT NULL DEFAULT '',             -- authored Markdown body (semantic body, no managed block)
  payload_hash TEXT NOT NULL,                -- today's _payload_hash, byte-identical derivation
  view_path TEXT NOT NULL,                   -- items layout: the item view path;
                                             -- log layout: '<log path>#<item_key>'
                                             -- governance path = view_path without '#...' (sec. 7)
  created_txn INTEGER NOT NULL, updated_txn INTEGER NOT NULL,
  UNIQUE (collection_id, item_key),
  UNIQUE (view_path)
)
CREATE UNIQUE INDEX items_natural_key ON items(collection_id, natural_key)
  WHERE natural_key IS NOT NULL;

item_versions(row_id, row_version, values_json, body, payload_hash, txn_id,
  PRIMARY KEY (row_id, row_version))         -- full history; supersession of values is queryable

item_sources(row_id, row_version, ordinal, source_ref TEXT NOT NULL,
  PRIMARY KEY (row_id, row_version, ordinal)) -- verified provenance references per version

txns(                                        -- one row per committed mutation = one transition
  txn_id INTEGER PRIMARY KEY,
  transition_id TEXT NOT NULL UNIQUE,        -- 24 lowercase hex, as today
  collection_id TEXT NOT NULL,
  operation TEXT NOT NULL,                   -- create|append|update|triage|revise|rebaseline|
                                             -- bulk_upsert|discard_held|view_edit|view_frame_edit|legacy_import|...
  profile_operation TEXT,                    -- plan_add, plan_update, ... for Planning wire names
  generation_before INTEGER NOT NULL, generation_after INTEGER NOT NULL,
  manifest_version_before INTEGER, manifest_version_after INTEGER,
  actor TEXT NOT NULL,                       -- audience/principal of the caller, or 'owner:view-edit'
  why TEXT NOT NULL,                         -- sanitized rationale, <= 512 bytes
  request_id TEXT UNIQUE,                    -- transport mutation_request_id or view-edit identity
  request_hash TEXT,                         -- canonical hash of the request; reuse with another hash refuses
  receipt_json TEXT NOT NULL,                -- the exact terminal receipt returned to the caller
  committed_at TEXT NOT NULL,                -- UTC, second precision
  prev_event_hash TEXT, event_hash TEXT NOT NULL,
  legacy_event_json TEXT)                    -- verbatim imported audit-v1/v2 event, else NULL

audit_effects(                               -- one row per changed item in a transaction
  txn_id, ordinal, row_id, item_key, effect TEXT CHECK (effect IN ('insert','update','held','resume')),
  effect_label TEXT,                         -- kind-dependent: correction|revision|replan|edit|transition|type_migration
  version_before, version_after, hash_before, hash_after, source_ref,
  PRIMARY KEY (txn_id, ordinal))

held_candidates(held_id TEXT PRIMARY KEY, collection_id, kind TEXT CHECK (kind IN
  ('write-refusal','view-correction')), candidate_json, diagnostics_json, view_path,
  base_row_version, created_txn, updated_at)

projection_state(path TEXT PRIMARY KEY, collection_id, row_id, kind TEXT CHECK (kind IN
  ('manifest','item','log','held','history')), rendered_generation INTEGER,
  rendered_row_version INTEGER, rendered_sha256 TEXT, state TEXT CHECK (state IN
  ('current','pending','held')))
```

**Invariants**
- `BEFORE UPDATE` and `BEFORE DELETE` triggers make `txns`, `audit_effects`, `item_versions`, `item_sources` and `collection_manifests` append-only. `items` rows are never deleted, because Records and Planning have no delete. Archival stays a Planning lifecycle value, and value supersession is the version history.
- `collection_type_versions` is append-only too.
- Every write runs in one `BEGIN IMMEDIATE` transaction under the existing writer lease, which stays the cross-process authority.
- The `txns` row is inserted last, with its final hashes. That is how the append-only trigger admits it.

**Identity and keys**
- `item_key` derivation is unchanged: an explicit key, else `uuid5` over the natural-key serialization (`structured_collections.derived_item_key`), else `uuid4`.
- The `natural_key` column stores the same serialization, so the unique index enforces exactly today's natural-key-conflict rule. A violation maps to `RECORD_NATURAL_KEY_CONFLICT` and names the holder, looked up by index.
- `AMBIGUOUS_RECORD` and `DUPLICATE_RECORD_ID` become unreachable for store-native data.

**Provenance per row**
- Who and why: `txns.actor`, `why` and `committed_at` for every version.
- Which sources: `item_sources`, holding verified references per version.
- Delivery evidence: validated as today, before commit.
- Declared `sources` link fields keep working unchanged, as ordinary values.

### 3. Guards: generations and row versions replace container hashes

- **Collection generation.** `collections.generation` increments once per committed transaction on the collection. That includes manifest revisions and view edits; it does not include holds.
- **Row version.** `items.row_version` increments once per change to that row.

The wire keeps its field names and 64-lowercase-hex shape, with domain-separated derivations:

```
container_hash = sha256("exomem-collection-generation:v1\0" + collection_id + "\0" + generation + "\0" + audit_head)
item_version   = sha256("exomem-collection-row:v1\0" + collection_id + "\0" + item_key + "\0" + row_version + "\0" + payload_hash)
manifest_hash  = sha256(manifest_text bytes)                       -- unchanged meaning
```

The old `before_item_hash` / `after_item_hash` receipt fields carry `item_version` values. A guard check is one indexed row read inside the transaction. Stale guards refuse with the existing codes: `STALE_RECORD`, and for Planning `STALE_PLAN_ITEM` / `STALE_PLAN_CONTAINER`.

**Query snapshot and continuation.** `snapshot` is the digest over the caller's authorized rows (`row_id`, `row_version`) plus `manifest_version`. On the uniform-release fast path (§7) it equals `container_hash`. A hidden-only change therefore never changes a released caller's snapshot or invalidates their continuation, which preserves the `governance-kernel` rule.

A guarded append still serializes with other writers on the same collection. At about 1 ms per write that no longer matters. An append keyed by natural key is also safe unguarded, because the unique index and content replay make it correct.

### 4. Idempotency and exactly-once

There are three layers, and the first two already exist:
1. **Transport idempotency** (the dispatcher's `mutation_request_id` and the per-vault idempotency store) is unchanged.
2. **Content replay** is unchanged in meaning. An append whose identity already holds an identical `payload_hash` returns `outcome: "replayed"` and writes nothing. It no longer needs an audit-chain correlation walk (`_replay_audit_correlation` is deleted): the store proves the row exists with that payload and was created by an `insert` effect.
3. **Store request identity (new, internal).** `txns.request_id` is `UNIQUE`, and `receipt_json` commits in the same transaction as the write. A retry that reaches the store with the same identity returns the recorded receipt. The same identity with a different `request_hash` refuses. That closes the one crash window the transport store cannot: a commit that happens before the transport ledger records it.

View edits use a deterministic identity, `sha256(view_path, base_row_version, sha256(file bytes))`, so a watcher replay after a crash is a no-op (§6).

### 5. Markdown projection

Every canonical object has a rendered view at today's path:

| View | Path | Edit-back |
| --- | --- | --- |
| Manifest | `<collection>/_collection.md` | yes, as a governed `revise` (§6) |
| Item (items layout) | `item_filename` recipe path, else `<source>/<item_key>.md` | yes, as a governed `update` (Planning: `update`, or `triage` for triage-only fields) |
| Log (log layout) | the declared log file | yes, per block: a changed block is an update; a new or removed block is held |
| Held candidate | `<collection>/Held/<held_id>.md` | no; read-only, resumed through the tool |
| History | `<collection>/_history.md`, plus `<collection>/_history/<YYYY>.md` | no; read-only, rewritten if touched |

**Log frame.** For the log layout, the prose outside the declared item section (headings, legend, notation), the BOM, the newline style and the final-newline state are stored in `collections.log_frame_json` and re-emitted byte-identically. An edit to the frame is recorded as a content-free `view_frame_edit` transaction, and it never touches items.

**Rendering** reuses today's renderers: `render_markdown_item`, `render_markdown_log_item`, the managed presentation blocks and the filename recipes. The system frontmatter stays (`type`, `collection_id`, `record_id` / `plan_id`, `schema_version`), so identity is visible in the file. The audit marker comments and the manifest `record_audit` / `plan_audit` mappings are no longer rendered.

**Views are normalized on re-render.** Frontmatter is emitted in canonical order and style. The authored body is canonical data (`items.body`) and is re-emitted exactly. Only YAML formatting that is not data (quoting style, key order, comments inside frontmatter) is not preserved. This replaces the byte-preservation contract of today's update splicer (**Needs ruling R5**).

**Item views are published before the acknowledgement; aggregate views follow asynchronously.** This follows #1457's measurement and recommendation: sync projection costs 2.6–3.8 ms at 1,000 and 10,000 items, while async projection lagged 4–34 ms and is unbounded under a stalled worker. Any reader of the vault file (`get_page`, a script, Obsidian, the next agent turn) therefore sees the write it was acknowledged for.
- **Changed item, manifest and held views are published synchronously.** Inside the transaction the writer renders each one to a target-adjacent staging file and fsyncs it, and marks its `projection_state` row `pending`. It then `COMMIT`s, renames the staging file over the view, and records `rendered_sha256`, `rendered_row_version` and `current`. Only then does it acknowledge.
  - A crash before `COMMIT` leaves only a staging file, which bounded crash recovery removes.
  - A crash after `COMMIT` and before the rename leaves the row `pending`, and the view is re-rendered on restart.
  - A view is never ahead of the store.
  - A rename failure, such as a Windows open-file lock, does not fail the committed mutation. The row stays `pending` and is retried, and the receipt carries a `projection_pending` warning.
- **Log views (log layout), history pages, per-year pages and type views** are rewritten asynchronously by one coalescing projector. They can be large, and they are not what a following read of the changed item needs. They use the same `pending` / staging / rename protocol with bounded lag, and are drained on restart and on quiesce.
- **Index sync of views is never on the acknowledgement path.** Lexical, resolver, memory-refs and graph sync of a published view goes to the derived drain. Records and Planning views are already excluded from recall, so a following `find` is unaffected. Structured readers (due-state, plan progress, capture sweeps, the working set, evidence bundles) read the store directly, so they have read-your-writes by construction (§15, item 7).
- `inspect` reports pending views (Planning: diagnostics code `PROJECTION_PENDING`; Records: an additive `projection` summary). Projection failure never fails a committed mutation. It raises attention and retries.

**History page.** It is generated from `txns` and `audit_effects` and readable like today's `log.md` entries: `## <date> <operation>`, then the actor, the reason, and wikilinks to the changed item views, newest first.
- The main page holds the latest 200 transitions. Older transitions go to per-year pages, and only the current year page is rewritten.
- It carries no item values, only content-free facts, as audit events do today.
- It is rendered for the collection-level release decision. Effects on rows that the collection-level audience could not read are omitted, and a transaction touching only such rows is omitted entirely, so the page never discloses more than its own path's release. Full per-row history remains available through `inspect` / `include_agent_history` under per-row authorization.
- Edits to history pages are ignored and the page is re-rendered. It is read-only by contract, and no edit-back exists for audit.

**Indexing.** Views stay ordinary vault files. Recall exclusion is unchanged (`recall_policy.is_recall_candidate` already excludes `Knowledge Base/Records` and `Knowledge Base/Planning` descendants except manifests). The lexical, resolver and graph sync of a published view goes to the derived drain, so it is not on the mutation's acknowledgement path.

### 6. Edit-back: the file watcher turns view edits into governed updates

`file_watcher` gains one hook: a changed path that `projection_state` owns goes to `collection_store.edit_back` instead of only the index publishers. Only the writer-lease holder applies edit-back. On other hosts the edit reaches the writer through vault replication and is handled there.

**Settling.** Editors autosave every keystroke burst. Edit-back acts on a path once it has been quiet for 2 s. A parse or validation failure is held only once the file has stayed invalid for 10 s. A later valid save supersedes that path's held correction, because the held id derives from `(view_path, base_row_version)`, so re-holding replaces it in place.

**Classification**, in order, for an item view:

| Observation | Result |
| --- | --- |
| `sha256(bytes) == rendered_sha256` | own write or no change; nothing happens |
| parses to the same values and body as the row | formatting-only edit; re-render, no transaction |
| `rendered_row_version != items.row_version` (row changed since this view was rendered, e.g. projection pending) | held `VIEW_CONFLICT` with both versions; the view is re-rendered to current and the human's bytes are kept in the held correction |
| parse failure, undeclared field, type or enum failure, system-field change (`collection_id`, id, `schema_version`), natural-key conflict, Planning lifecycle or hierarchy violation | held `VIEW_INVALID` with the field-addressed diagnostics the tool would return |
| valid change to declared values and/or body, current base version | governed `update` (Planning: `update`; `triage` when only triage fields changed) through the ordinary writer: same validation, governance precommit, audit transition, receipt; actor `owner:view-edit`, `why: "edited view <path>"`, request identity per §4; then re-render |
| file deleted | held `VIEW_DELETED`; the view is re-rendered, because no delete exists |
| file moved or renamed | the new path is an unbound file (next row); the canonical path is re-rendered |
| unbound `.md` file created under a projection root | held `VIEW_UNBOUND` as a proposed insert, resumable with `append(held=...)` for Records or `add` for Planning |
| sync-conflict copy (`*.sync-conflict-*`, `* (conflicted copy)*`) | held `VIEW_CONFLICT_COPY`; never applied |

A **manifest view** edit becomes a governed `revise` when it validates as a revision of the current `manifest_version`. Otherwise it is held, with the same classification. Schema-breaking revisions follow today's revise rules. A **log view** is diffed block by block using the `exomem-record-id` binding. A changed block is an update. A block with no binding, or a removed block, is held. Reordering is ignored and re-rendered. Held **view corrections** appear in `inspect` coverage, in the attention queue, and as read-only views under `Held/`. They are resolved by resuming, by discarding with a reason, or implicitly by a later valid edit of the same view.

The watcher is the human's write path, not a bypass. It uses the same leaf functions as MCP, REST and CLI, so the surface-consistency rule holds.

### 7. Governance: row-level, resolved once, enforced in the query layer

**Governance subject per row.** Each row is evaluated as the tuple:
- `path`: its `view_path`, the same path its file has today;
- `ref`: `exomem://<item_type>/<cid>/<key>` (`record`, `plan`, or a declared type's `item_type`, such as `recipe`);
- `type`: the type's `item_type`;
- `tags`: the values of a schema-declared `tags` field, if any;
- `project`: the project of the manifest;
- `default_deny`: the subject-level default from its type's `default_audience` (§14.6).

The existing pure evaluator, scopes (`paths`, `refs`, `tags`, `types`, `projects`, `classes` and their excludes), standing rules, grants and org caps apply unchanged. **Row-level audience** is therefore authored exactly like any other policy, in `_Governance`, for example with a scope selecting a row's `ref` or `tag`, or a path under the projection root.

No per-row audience column is added. Governance stays in one authored place, and a data write can never widen or narrow its own release (**Needs ruling R3**, since the brief says "row-level audience").

**Resolved once per operation.** The policy, tombstones and state paths are resolved once per call, which is #1457 fix 1. Every row then evaluates against that resolved policy in memory.

**Uniform-release fast path.** A collection is uniform for an audience when no scope in the resolved policy can distinguish its rows. That means no path selector matches strictly below the projection root, no ref selector names one of its rows, and no tag or class selector can match a row-declared value. Then one decision covers the manifest and every row:
- L6: the whole collection is visible, and filters, sort, limit and aggregates may push down into SQL.
- Below L6: the collection is absent.

**Per-row path.** Otherwise, the query runs in two phases:
1. An identity-only projection (`row_id`, `view_path`, `item_key`, tags) is authorized per row. No value is decoded before this step.
2. The authorized set goes into a temporary table, and filtering, sort, pagination, totals, aggregates, hierarchy and continuation are computed only over it.

The same set feeds `snapshot` (§3). Withheld rows are indistinguishable from absent rows: they do not count, bound caps, create ambiguity or change continuations.

**Composition with existing governance**
- **Mutation** still requires the complete authorized state. If any row of the collection is withheld from the caller, append, update, bulk upsert, revise and rebaseline refuse `COLLECTION_NOT_FOUND`, as they do today. Relaxing this would let a uniqueness conflict or a generation bump reveal a hidden row. It is now a cheap check: the uniform-release decision, or one identity-only pass.
- The precommit governance hook (`precommit_authorize_mutation`) runs inside the store transaction before `COMMIT`. A refusal rolls the transaction back, so no row, transition or receipt exists.
- The envelope projectors (`project_query_result`, `project_mutation_receipt`, `_LinkProjector`) are unchanged. They run over the store results.
- **Egress of view files** through `get_page`, `find` and other tools resolves the file to its rows. An item view is released exactly as its row. A log view is released only when every row it renders is released to the caller; otherwise the whole file is withheld, so withheld stays absent. Filesystem readers are the owner, who is not subject to scope defaults.
- **Held candidates** keep their rule: they are filtered like items before being disclosed or counted.
- **Hosted cells** use the same code path.

### 8. Tool contracts

`record_memory` and `plan_memory` keep their tool names, actions, argument sets, argument-validation messages and receipt field names. Every contract change:

| # | Surface | Change | Compatibility |
| --- | --- | --- | --- |
| C1 | `expected_container_hash`, `after_container_hash`, `before_container_hash`, `snapshot`, `source_versions[].hash` | values derived from generation and row sets (§3), still 64-hex | a caller holding a pre-migration value gets one stale refusal and refreshes |
| C2 | `expected_item_version`, `item_version`, `before/after_item_hash` | derived from row version and payload hash | same as C1 |
| C3 | `source_versions` | entries name view paths as today; the hash is the row digest, not file bytes | same shape |
| C4 | `inspect.audit` | status values unchanged. Store-native collections report `ok` / `baseline`. `gap` and `acknowledged_gap` come only from imported legacy history or `history_incomplete` from governance. Records `inspect` gains an additive `projection` summary (pending views, held view corrections). Planning `inspect` keeps its exact key set and reports these as diagnostics codes | additive for Records; unchanged keys for Planning |
| C5 | `inspect.diagnostics` | new codes `PROJECTION_PENDING`, `VIEW_CONFLICT`, `VIEW_INVALID`, `VIEW_DELETED`, `VIEW_UNBOUND`, `VIEW_CONFLICT_COPY`. The `DUPLICATE_RECORD_ID` / `AMBIGUOUS_RECORD_KEY` / `filename_drift` codes become unreachable for native data | additive |
| C6 | `rebaseline` | kept, with its guards and v2 receipt; valid only to acknowledge imported legacy gaps. On a collection with no gap it refuses with the existing mismatch code, because the acknowledgement cannot match | frozen profiles keep the action |
| C7 | `revise` | unchanged arguments. The manifest is canonical in the store and `_collection.md` is its view | unchanged |
| C8 | Direct edits | no longer reported as audit gaps. They become governed transitions (`operation: view_edit` in history, mapped to `update`/`triage` for Planning wire names) or held corrections | behaviour change, specified |
| C9 | `Knowledge Base/log.md` | stops receiving `Records audit-v1` / `Planning audit-v1` lines. Per-collection history pages replace them. Existing lines stay as immutable history | behaviour change, specified |
| C10 | Held candidates | the same `held` / `hold` / `discard` arguments and references; storage is a table with read-only views at today's `Held/` paths | unchanged wire |
| C11 | `describe` | storage section explains the store, views and edit-back; authoring contract unchanged | text only |
| C12 | `bulk_upsert` (PR #1452, not yet released) | one transaction and ONE transition with N `audit_effects`. `first_transition` equals `last_transition`. `BULK_UPSERT_AUDIT_DEPTH` and the chain-depth budget are removed. Everything else in #1452's ratified contract stands | pre-release |
| C13 | size limits | `_MAX_ITEM_FILES` (2,000), `_MAX_COLLECTION_BYTES` (8 MB) and `_MAX_RECORDS` (10,000) stop bounding store collections. The new limit is `COLLECTION_ROW_LIMIT` = 100,000 rows per collection, exposed by `describe`. A log-layout view keeps its 2 MB rendering cap; a log collection past it refuses the write with a remediation to switch to the items layout | limit raised |
| C15 | `schema_memory` | new `subject: "collection-types"` with `inventory`, `inspect`, `validate`, `diff`, `save-collection-type`, `history` and `restore`; `infer` refused (§14.5) | additive; frozen candidates unchanged |
| C16 | `record_memory` | per ruling R8: also serves collections of declared types, with the new `action: "transition"` and an optional `collection_type` on `describe` | additive on the local surface and v5 only |
| C17 | `plan_memory` | a facade over the generic operations (§14.7); wire unchanged | unchanged |
| C18 | manifests | `collection_type:` names the type; `semantic_profile: records\|planning` remain accepted aliases; `link` fields may declare `target.collection_type` and `pin: version` (§14.3) | additive |
| C19 | `activate_context` packet | `generation.collection_types_hash`; the `collections` lane replaces the `records` and `planning` lanes (both kept as aliases); `ANCHOR_KINDS` gains `item` | additive |
| C14 | receipts | `receipt_version: 1` shapes unchanged. `audit_correlation` is the 24-hex `transition_id` of the one transaction | unchanged |

**Frozen hosted candidates** are unchanged. `hosted_legacy_profile_schemas.json` pins v1–v4 and `test_hosted_legacy_profile_pin.py` re-derives the pin. `minimum_records_reader_version` stays 2: `audit_reader_version` (1 or 2) is kept per collection and reported as today, and the store has its own `schema_version` that the wire does not expose. The local surface, the v5 candidate, the command binding and the derived artifacts are regenerated only for C11 and C12.

### 9. Bulk upsert is one transaction

PR #1452's `bulk_upsert` runs in one `BEGIN IMMEDIATE` transaction:
1. check the guard once;
2. plan the rows in input order against the transaction's own view;
3. write inserts and updates;
4. insert one `txns` row with N `audit_effects`;
5. `COMMIT`.

`abort` rolls back on any rejection and reports every row's would-be outcome. `skip` commits the accepted rows. A transaction with only `unchanged` rows writes nothing and does not advance the generation. The 500-row bound stays. The spike measures 20 ms for 500 rows (250 inserts, 250 updates) at 10,000 existing rows, and 6 ms for a full-replay batch.

The option-B "bulk audit event" that #1452 deferred is simply the native shape here. **Needs ruling R2** covers the sequencing with #1452.

### 10. Migration: verifiable, reversible, zero-downtime

**Switch.** A per-vault store mode lives in `store_meta` and the state root: `files` (the default until migrated) or `store`. Code for both ships in the same release. File mode keeps today's machinery until the deletion phase (§13). This is the default-off seam.

**Preflight (`maintain_memory(mode="collections-store", dry_run=true)`)** reports, per collection:
- the rows found;
- the legacy audit status;
- the round-trip result;
- blockers.

Blockers are duplicate or ambiguous identities, schema violations, unsupported versions, and datasets (which are skipped, since they stay files). A vault with blockers does not migrate. The owner fixes them in files first, as today. This step is read-only and changes no file.

**Import and verification** (the migrator, pure and deterministic). For each collection:
1. Read the files with today's adapters (`MarkdownItemsAdapter` / `MarkdownLogAdapter`), with full authority.
2. Write the rows with the same `item_key`, `natural_key`, `values_json`, `body`, `payload_hash` and `view_path`. Write the manifest text verbatim as `manifest_version` 1, and the held candidates with their existing ids.
3. Parse the legacy events from `log.md` and the archives using a read-only legacy parser, which is the extracted `_audit_events` / `_audit_event_syntax` / `_reconstruct_audit_chain`. Import every reachable event as a `txns` row with `operation: legacy_import`, `legacy_event_json` verbatim, and `prev_event_hash` chaining. Then append one `legacy_import` checkpoint transaction. `legacy_audit_status` is set to the legacy inspector's result, so a `gap` stays a `gap` and is never blessed.
4. **Round-trip proof.** It must pass for the vault to migrate:
   - (a) row count equals legacy item count;
   - (b) for every row, `parse(render(row))` equals the row's values, body, key and natural key;
   - (c) `payload_hash` recomputed equals the legacy `_payload_hash`;
   - (d) the imported chain head equals the manifest `record_audit` / `plan_audit` head, and the event count equals the legacy chain length;
   - (e) the manifest text is byte-equal;
   - (f) the legacy inspect status equals `legacy_audit_status`.
   Render drift, where `render(row)` differs in bytes from the current file, is recorded, is not a failure, and changes nothing. Files are not rewritten at migration.
5. Record `projection_state` with `rendered_sha256` equal to the **current file hash**. The existing files are therefore already "current views", and a later human edit is detected against them.

**Zero downtime** uses `managed-service-upgrades`:
1. The target release declares the offline migration `collections-store-v1`.
2. The standby worker, which holds no lease and no ownership, pre-imports into a staging store from the live vault read-only, and records the per-collection basis: the legacy container hash and audit head.
3. The active worker keeps serving reads and writes.
4. At handoff, after the old worker has provably exited, the migrator re-checks each basis. Collections that changed in the meantime are re-imported, which is a small delta. The whole round-trip proof re-runs, the store is published atomically into the state root, and the mode is set to `store`. Then the standby is promoted.
5. Writes pause only for the ordinary bounded handoff. A failed proof leaves the mode at `files`, the vault untouched and the old release restorable. The upgrade reports the failing collection and check.

**Reversal**
- **Before the first store write:** set the mode back to `files`. The files and their legacy chains were never touched.
- **After store writes:** `maintain_memory(mode="collections-store-export", dry_run=...)` renders every row back into the legacy canonical layout:
  - item and log files with audit markers;
  - manifests with `record_audit` / `plan_audit` heads;
  - per collection, one legacy-valid v2 `rebaseline`-shaped checkpoint event naming the store transitions it summarizes, content-free.

  Legacy inspect then reads `acknowledged_gap` with the discontinuity documented, never a silent `ok`. The export is atomic per collection through `batch_atomic_write` and preview-first. After it runs, the mode is set to `files`.
- **Test:** files → store → export → files is byte-equal for collections not written in store mode, and legacy-valid for collections that were.

### 11. Backup, restore and hosted cells

- **Consistent snapshot primitive.** `sqlite3.Connection.backup` into a staging file, `journal_mode=DELETE`, `PRAGMA integrity_check`, fsync, atomic rename. The vault replica (§1) uses it, and so do `exomem collections backup --to <file>` (and `--stdout` for `restic backup --stdin`) and portability export. The spike measures 77 ms at 10,450 rows (15.8 MB with history).
- **restic and vault copies** back up the vault. The replica is a single consistent file, and the WAL files of the live store are never in the vault. Operators who back up the state root directly are told to use the `backup` command or to exclude `-wal` / `-shm`, because copying a live WAL database is not consistent.
- **WAL checkpoint.** A passive checkpoint runs after the projector drains, and a truncating checkpoint runs on quiesce, so the WAL stays bounded.
- **Restore.**
  - Check the snapshot's `integrity_check`, `schema_version`, and that its `store_id` matches or restore is explicit.
  - Re-render every view into a staging tree and compare it with the vault.
  - Publish the store, then drain the projector.
  - A restored store older than the vault's views surfaces the difference as held `VIEW_CONFLICT` corrections, never as silent overwrites.
- **Hosted cells.** The live store sits in the cell state root on the tenant volume (`external-canonical`), one per cell, so tenant isolation is unchanged. Quiesce flushes the replica. Portability export includes the quiesced snapshot at its vault path as canonical data, excluding `-wal` / `-shm`. Restore stages and validates as above before publication. `hosted-vault-portability` is modified accordingly.

### 12. Performance

**Targets**, in the release acceptance harness, as extensions of `scripts/measure-records-append-latency.py` from #1457:

| Operation | Target | Today on `main` (#1457) | Spike (storage engine only) |
| --- | --- | --- | --- |
| guarded append, p95, N=10,000 | < 20 ms end to end | 5,078 ms at N=1,000 (append only); 12.2 s round trip | 0.72 ms p95 (0.83 ms at N=1,000) |
| bulk upsert 500 rows | < 1 s end to end | ≈ 500 serial appends | 20 ms; replay 6 ms |
| query, filter + sort + limit 50, N=10,000 | same results as the file path; p95 ≤ 50 ms per-row governed, ≤ 5 ms uniform | 7.6 s refresh read at N=1,000 | 44 ms per-row path, 0.4 ms uniform fast path |
| snapshot, N=10,000 | < 250 ms, off the ack path | n/a | 77 ms |
| item view render + staging fsync + publish | on the ack path, < 4 ms p95 | inside the write | 0.6 ms p95 (spike); 2.6–3.8 ms p50/p95 for the whole sync-projection storage layer (#1457 prototype) |
| guard refresh as a command (inspect-lite) | p95 < 15 ms | 416 / 546 ms at N=1,000 after #1457's fixes | 0.02–0.04 ms storage (#1457 prototype) |

The spike numbers come from a 4-core container with ext4, SQLite 3.45.1, WAL and `synchronous=FULL` (`benchmarks/collections_sqlite_spike/results-*.json`). They exclude the dispatcher, the idempotency ledger, governance resolution and receipt projection.

**End-to-end append budget.** #1457's SQLite prototype confirms the storage layer is flat: 2.6–3.8 ms with synchronous projection at both 1,000 and 10,000 items. Its end-to-end estimate of about 93/100 ms p50/p95 is dominated by about 90 ms of shared cost outside storage, which that composite carried over unchanged from today's writer. That 90 ms is the real target of the 20 ms goal. The budget, per stage at p95:

| Stage | Today (#1457, after its fixes, 10 items) | Store design | How |
| --- | ---: | ---: | --- |
| Dispatcher, idempotency ledger, egress filter, digest | 7 ms | ≤ 5 ms | unchanged code; the ledger's fast-ack already sits inside it |
| Writer lease / mutation boundary enter and exit | ≈ 2 ms | ≤ 2 ms | unchanged |
| Collection resolution and manifest | 15 ms (78 ms before the parse cache) | ≤ 1 ms | a `collections` row lookup by name or id, plus a parsed-contract cache keyed by `(collection_id, manifest_version)`; no manifest discovery, file read or YAML parse on the write path |
| Request validation | ≈ 2 ms | ≤ 2 ms | unchanged; validators come from the cached contract |
| Governance precommit | ≈ 5 ms | ≤ 2 ms | policy resolved once (#1457 fix 1); uniform-release decision, or one identity-only pass (§7) |
| Store transaction plus synchronous item view | ≈ 60 ms of guard and write work | ≤ 4 ms | measured 2.6–3.8 ms (#1457 prototype), 0.7 ms without the view (this spike) |
| Post-commit fan-out: index sync and self-write registration | ≈ 50 ms (86–133 ms before) | 0 ms on the ack path | moved to the derived drain, since views are recall-excluded (§5) |
| Due-state and capture-sweep carriers | ≈ 3 ms | ≤ 2 ms | computed from the store row instead of an adapter re-read |
| Receipt projection | ≈ 1 ms | ≤ 1 ms | unchanged |
| **Total** | ≈ 155 ms p50 (157 ms at 10 items) | **≤ 19 ms p95** | |

Each stage gets its own timer in the acceptance harness, and a stage over budget fails the release gate (task 10.1). The collection-resolution and fan-out rows are where the 90 ms goes. Both are removed by the store design itself, not by tuning.

The guard refresh a client performs between writes is also a command, so it pays the dispatcher. #1457 estimates 15–25 ms, against a storage cost of 0.04 ms. Two things make it rare and cheap:
- every mutation receipt already returns `after_container_hash` and `item_version`, so chained writes need no refresh;
- `record_memory(action="inspect")` gains no work, but its guard fields come from the `collections` row, not a snapshot (target p95 < 15 ms).

Removed from the acknowledgement path:
- the full collection read (≈ 4.3 ms per item);
- about 20 directory censuses and nine `lstat` guard rounds per item;
- manifest discovery and parsing;
- `log.md` read-rewrite;
- synchronous view index sync (≈ 50–133 ms), which moves to the derived drain.

**Query parity.** The parity path runs `query_data.evaluate_rows` over the rows the store returns, so filter operators, NFC normalization, type coercion and aggregates are the same code as today. SQL push-down is used only on the uniform-release fast path, and only for operators with a passing parity test against the file adapter on a generated parity corpus. The fast path covers eq, ne, lt/lte/gt/gte on declared scalar fields, sort, limit and count/min/max/sum/avg. Any other operator falls back to the parity path. Declared-field expression indexes are created per collection for the fields its saved views filter or sort on.

### 13. What is deleted

Removing this machinery is a goal. It is deleted in the last phase, after the migration window. Until then file mode keeps it behind the switch. The read-only legacy parser needed by the importer and the reverse exporter is kept in `collection_store/legacy.py`. Everything else goes:

**Container hashing and file guards on collection writes**
- `records._items_container_hash`, `_container_hash_with_replacements`, `_empty_items_container_hash`, `_absent_source_container_hash`, and the Markdown-log whole-file container hash.
- The collection writers' `PathGuard` / `DirectoryCensusGuard` capture and their `vault.batch_atomic_write` use. `batch_atomic_write` itself stays for knowledge notes.
- `_publication_error`'s residue mapping for collections (`RECORD_RECOVERY_REQUIRED`).
- The writer-side casefold and portable-path collision checks (`_assert_portable_absent`, `_casefold_alias`). Path allocation for views moves into the projector, which keeps `render_item_path` collision suffixes.
- `record_formats._snapshot` inventory hashing and its recursive census.

**Hand-built uniqueness**
- `records._natural_key_twins` and `_natural_key_moved` scans: the unique index replaces them, with a lookup only to name the holder.
- `record_formats._mark_ambiguous` and the `AMBIGUOUS_RECORD` / `DUPLICATE_RECORD_ID` write refusals for native data.

**Audit markers and heads**
- The `# exomem-record-audit:` / `# exomem-plan-audit:` item comments and `<!-- exomem-record-audit -->` log comments on write.
- `record_formats.render_manifest_audit_head` and `_yaml_head_scalar`, plus head recovery.
- `structured_collections._audit_head` and `plan_audit_head` as authorities.
- `records._structural_audit_marker`.

**`log.md` audit protocol (writer and live reader)**
- `records._audit_body`, `_lifecycle_audit_body`, `_plan_required_audit`, `_require_activity_log`.
- The collection callers of `vault.plan_log_writes` and the archive rotation for those events.
- The live `_audit_events` log scan with its 8 MB / 10k caps.
- `_reconstruct_audit_chain` with its 2048-depth cap.
- `_inspect_audit_chain` and `inspect_audit_gap`, rewritten as table queries.
- `_audit_event_syntax` (kept only in `legacy.py`).

**Replay correlation**
- `records._replay_audit_correlation`.

**Whole-collection re-reads per write**
- The adapter full read and full re-authorization inside `append_record` / `update_record` / `_lifecycle_mutation`.
- `record_governance.require_mutation_visibility`'s directory walk, which becomes one identity-only authorization pass or the uniform-release decision.
- `due_state._unfiltered_snapshot`'s adapter reads, which become store reads.

**File adapters as canonical readers**
- `MarkdownItemsAdapter` / `MarkdownLogAdapter` stop being read paths. Their parsing functions are kept as the edit-back and import parsers.
- `render_markdown_item_update`'s byte-preserving splice.
- The file-inventory size caps (`_MAX_ITEM_FILES`, `_MAX_COLLECTION_BYTES`, `_MAX_RECORDS`) for store collections.

**Held files as storage**
- `records.hold_candidate`'s file writer, `held_census`'s directory scan, and `discard_held`'s file deletion. All become table operations with rendered views.

**Representation-migration receipts**
- `structured_files`' JSON receipts under `_Governance/structured-files/` become store transactions. `structured_files` preview/apply becomes projection-only maintenance (filename recipes), because it no longer touches canonical data.

**Hard-coded profiles**
- The closed two-profile model: `collection_profiles.RECORDS_PROFILE` / `PLANNING_PROFILE` / `PROFILES`, `structured_collections._SUPPORTED_PROFILES`, `_require_records_layer`, and `_require_profile_layer`'s literal layers. These are replaced by the type registry, with the built-in wire names moving into `records.yaml` / `planning.yaml`.
- The `{"Records","Planning"}` literals in `recall_policy` (`:289-298`), which now read the type registry's placements.
- The `semantic_profile == "records"|"planning"` branches in `records.py`, `record_governance.py`, `record_formats.py`, `planning.py`, `due_state.py`, `audit.py`, `structured_files.py`, `plan_progress.py`, `working_set_index.py` and `working_set_state.py`. There are 146 `semantic_profile` occurrences across 14 modules on `main`. Each becomes a kind check, a declaration lookup or a facade map.
- The `records` / `planning` arms of `working_set._lane` and the planning-only anchor path. These are replaced by the one generic `collections` lane and item anchors.

**Related specs**
- The `structured-collections` requirements whose subject is the file audit protocol (marker, head, activity-log event) are rewritten by the deltas in this change.

`dataset` adapters, knowledge-note writers, `batch_atomic_write`, the governance evaluator and the query evaluator are not deleted. The size of the deletion is measured in the deletion task, not estimated here.

### 14. One collection mechanism; Records and Planning are built-in types

Today `collection_profiles.py` hard-codes exactly two profiles (`RECORDS_PROFILE`, `PLANNING_PROFILE`), and the product boundaries branch on `semantic_profile`. In the store, a **collection type** is data. The mechanism is one generic implementation:
- identity, natural keys, guards, transactions and audit (§2–§4);
- projection and edit-back (§5, §6);
- row-level governance (§7);
- query, snapshots and migration.

Records and Planning are the two **built-in** types, shipped as declarations. A user or agent can declare a new type in conversation, without code, and its collections immediately get every capability above plus context-compiler surfacing.

#### 14.1 What a type declaration says

```yaml
name: recipes                 # type id: lowercase, [a-z][a-z0-9-]{1,39}, unique, immutable
version: 1                    # assigned by save; monotonically increasing
title: Recipes
item_type: recipe             # singular noun; also the reference namespace: exomem://recipe/<cid>/<key>
kind: procedural              # closed vocabulary, see 14.2
placement: Recipes            # one new segment under the Knowledge Base: Knowledge Base/Recipes/
description: Canonical how-to for dishes; the current revision is what gets followed.
fields:                       # the same field types and limits as manifest item_schema today
  title:         {type: string, required: true}
  serves:        {type: integer}
  total_minutes: {type: integer}
  ingredients:   {type: array, items: {type: string}, required: true}
  steps:         {type: array, items: {type: string}, required: true}
  tags:          {type: array, items: {type: string}}
natural_key: [title]
extensible: false             # may a collection of this type add its own fields?
lifecycle:                    # optional; one state field, a closed state machine
  field: status
  initial: draft
  states: [draft, current, retired]
  transitions: {draft: [current, retired], current: [retired], retired: [current]}
  serve_states: [current]     # only these states are surfaced by the compiler
  constraints:                # optional declarative per-state field rules
    - when: {status: [current]}
      require: [ingredients, steps]
surfacing:                    # when a turn should be served this type's items (14.4)
  cues: ["how do i make", "recipe for", "how long does", "what do i need for"]
  match_fields: [title, tags, ingredients]
  anchor_kinds: [item, collection]
  max_items: 2
default_audience: owner       # owner | policy (14.6)
presentation:                 # the existing recipes, now declared on the type
  item_filename: {fields: [title]}
  item_presentation: {summary: [serves, total_minutes], long_text: [ingredients, steps]}
views:                        # saved views, validated against this type's vocabulary
  - {name: serving, filters: [{column: status, op: eq, value: current}]}
```

**Validation** is closed and field-addressed, like manifest validation today.
- Unknown keys, an unknown `kind`, a `placement` that collides with an existing layer, a reserved directory or a non-empty ordinary directory, a natural key naming undeclared fields, and an unreachable or undeclared state are all findings.
- A declaration with any finding cannot be saved.
- A declaration carries no code, templates-as-code, regexes beyond the bounded cue strings, or model instructions.

**Collections of a type.** A collection manifest names its type (`collection_type: recipes`). `semantic_profile: records` / `planning` remain accepted aliases for the built-in types.
- If the type is `extensible`, the manifest may add fields and, when the type declares none, its own natural key. This is how Records works today: the built-in `records` type is a thin observed ledger whose collections declare their own fields.
- Otherwise the type fixes the schema, and each collection is one instance, for example "Weeknight recipes" and "Baking" as two collections of `recipes`.

**Built-in declarations.** `records` and `planning` ship as package data (`src/exomem/_collection_types/records.yaml`, `planning.yaml`) and change only with a release.
- `records` is `kind: observed`, `placement: Records`, `extensible: true`, with no fixed fields.
- `planning` is `kind: intended`, `placement: Planning`, with today's core Planning fields, lifecycle vocabulary and six horizon views. Its hierarchy rules reference the named validator `planning.hierarchy.v1` (below).
- Only built-in declarations may carry a `wire` block: legacy property and receipt names (`record_id` / `plan_id`, `_record_receipt` / `_plan_receipt`), error-code remaps (`STALE_PLAN_ITEM`, ...) and verb aliases (`add` → `append`, `triage` → `transition`). That is what keeps the two existing tools byte-compatible (14.7).
- Saving a declaration named `records` or `planning` refuses with `BUILTIN_COLLECTION_TYPE`.

**Named validators.** Some rules are not expressible declaratively, such as Planning's typed hierarchy (an initiative's parent must be an outcome, no cycles, area agreement). These are product-owned, versioned, code-owned validators in a closed registry: `planning.hierarchy.v1`, `references.acyclic.v1`. Any declaration, built-in or declared, may opt into a registered validator by name. A declared type never supplies code.

#### 14.2 Kind: the only semantic switch

`kind` is a closed vocabulary. It is the only field that changes behaviour beyond the schema, and it changes exactly two things: **version semantics** and **compiler surfacing**. Storage, keys, guards, transactions, audit, governance, projection and edit-back are identical for every kind.

| | `observed` (Records) | `intended` (Planning) | `procedural` (Recipes) | `reference` |
| --- | --- | --- | --- | --- |
| Meaning of an item | a fact or event as observed | intended future state | a canonical way to do something | stable facts to look up |
| An update to values or body is labelled | `correction` of that observation | `replan` | `revision`; revision N supersedes N−1 | `edit` |
| Current version served | the item as last corrected; collections read newest-observation first | items in active lifecycle states | the current revision of items in `serve_states` | the current version |
| History and view footer | "corrected on …" | status and horizon changes | "Revision N, supersedes N−1 (date, why)" | "edited on …" |
| Default compiler roles | `current_state`, `recent_change`, `baseline`, `evidence` | `active_plans` | `methods` | `resources` |
| A pinned reference to one of its items | an observation as it stood | a plan as it stood | the revision that was followed (the common case) | a version as cited |

Every change is still a new `row_version` in `item_versions`, one `audit_effect`, and one transition, whatever the kind. The kind only chooses the label (`audit_effects.effect_label`), the history wording, the default query semantics, and which roles serve the items.

A declaration may narrow its kind's roles (`surfacing.roles`, a subset). It may not name roles outside them, so a procedural type cannot present itself as `constraints` or `identity`.

The vocabulary grows only by shipped revision (**Needs ruling R9**), because each kind needs a compiler mapping and version wording.

#### 14.3 Pinned version references

A `link` field may target a collection type and declare `pin: version`:
- its value is `exomem://<item_type>/<collection_id>/<item_key>@<row_version>`;
- an unpinned link, or `pin: current`, stores no version and resolves to the current row.

Resolution of a pinned link reads `item_versions`, which already hold every version. The pinned target is authorized as the item itself (the same governance subject), so a withheld target projects to `None`, exactly as `_LinkProjector` does today. Write-time validation checks that the pinned version exists and was authorized for the writer. The natural key and `payload_hash` include the full pinned value, so two runs that followed different revisions are different observations. Query `group` and `filters` may use the pinned value, or its two parts: `<field>.item` and `<field>.version`.

#### 14.4 Surfacing: when a turn is served a type's items

Today the compiler knows exactly two collection shapes:
- `context_roles.LANES` has a `records` lane and a `planning` lane (`context_roles.py:68`), and `working_set._lane` dispatches them with a hard-coded chain (`working_set.py:727-758`).
- Records collections become `collection` anchors, and only Planning items become item-level (`plan`) anchors (`working_set_index._collection_candidates` / `_planning_candidates`, `working_set_index.py:1188-1287`).
- `current_state` is the only role backed by Records (`working_set_state.current_state_for`).

This change makes the compiler generic once, in a shipped registry and code revision, so that no later type needs code:
- **One `collections` lane.** It replaces the `records` and `planning` lanes, and both names stay accepted aliases for vault overrides. The lane serves items of the collection kinds its role names.
- **Roles gain an optional second source, `collection_kinds`, beside their existing lane.** The role vocabulary is unchanged:
  - `current_state` → `[observed]`, which replaces lane `records`;
  - `active_plans` → `[intended]`, which replaces lane `planning`;
  - `methods` keeps `units` `[technique, design]` and gains `[procedural]`;
  - `resources` gains `[reference]`.
- **Anchors come from the type registry, not from profile literals.** `_collection_candidates` emits a `collection` anchor per collection, and item-level anchors for every type whose `surfacing` declares `match_fields`. The closed `ANCHOR_KINDS` gains `item`, and intended items keep anchor kind `plan` for compatibility.

A type's `surfacing` block does not edit the registry. It scopes selection within its kind's roles:
- `cues`: bounded, normalized strings (at most 16, each at most 64 characters) evaluated by the same deterministic cue matcher. A cue hit selects the kind's roles and makes this type a candidate source.
- `anchor_kinds`: resolved anchors of these kinds (`item`, `collection`, `plan`, `resource`, `project`) make this type a candidate source. `match_fields` build the item anchors' terms, so a turn mentioning "lentil soup" resolves to that recipe's item anchor through the ordinary anchor resolver.
- `max_items`: a per-type cap inside the role's existing budget.
- Only items in `serve_states` (default: every state that is not terminal) and at the kind's current version are served. Retired recipes are never served, and an old revision is never served unless a pinned reference is being expanded.

Selection stays deterministic, with no model call. Retrieval scorers may **rank** candidates, as they do today; that is permitted by the pure-substrate authority matrix. Declarations are saved only through the owner-confirmed `schema_memory` write path, and no server component authors or edits them. The packet's `generation` block gains `collection_types_hash` beside `roles_hash`, so a changed surfacing rule is visible in every packet, preserving the `context-roles` "review-gated evolution" rule.

#### 14.5 Authoring, versioning and migration through `schema_memory`

There is no new tool. `schema_memory` gains `subject: "collection-types"` with the same operation pattern the `context-roles` subject already uses (`commands.op_schema_memory`):

| Operation | Arguments | Effect |
| --- | --- | --- |
| `inventory` | none | every type (built-in and declared) with version, kind, placement, collection count; authorized counts only |
| `inspect` | `name` | the current declaration, its version history summary, derived placement and roles, and collections of the type |
| `validate` | `proposal` | closed findings; no write |
| `diff` | `proposal` (or `name` + held type-view reference) | findings, the change class (below), and an impact preview: per collection, how many items change or fail and the first failing items with field paths. `content_hash` of the current version |
| `save-collection-type` | `proposal`, `why`, `expected_hash` (required when the type exists; must be absent for a new type), optional `scaffold` (create a first collection) | refuses on any finding; else one store transaction (below) |
| `history` | `name` | versions, newest first: time, why, before and after hash, change class |
| `restore` | `name`, `version`, `why`, `expected_hash` | re-saves that version's declaration as a new version, through the same rules |
| `infer` | — | refused, since the server does not propose types |

**Storage.** Declarations are canonical in the store:
- `collection_types(name PRIMARY KEY, current_version, builtin INTEGER)`;
- `collection_type_versions(name, version, declaration_json, declaration_hash, change_class, txn_id)`, which is append-only.

`collections` gains `type_name` and `type_version`, replacing the `profile` column of §2. Each type is rendered as a read-only view at `Knowledge Base/_Schema/collection-types/<name>.md`. An edit to that view is never applied, because a type change can migrate items and needs its `diff` preview first. The edit is held as a type proposal that `diff` can load by reference, and the view is re-rendered.

**Change classes**, computed by `diff`:
- **compatible**: add an optional field; add a state or transition; widen an enum; change `surfacing`, `presentation`, `views`, `description`, `title` or `serve_states`; tighten `default_audience`. No item changes. Collections re-bind to the new version and views re-render.
- **migrating**: add a required field, rename or drop a field, narrow an enum, remove a state, change a field type, or change `natural_key`. The declaration must carry a `migration` block from a closed set of steps: `rename_field`, `map_values`, `default_value`, `drop_field` (values stay in `item_versions` history), `convert` (integer↔number, string→enum via `map_values`, scalar→array), and `recompute_natural_key`.
- **refused**: changing `name`, `item_type`, `kind` or `placement`. Identity and meaning are immutable; a different kind is a different type.
- **release-widening**: `default_audience` `owner` → `policy`. Saved only by the owner principal (14.6).

**Save is one store transaction.**
1. Insert the new type version.
2. For every collection of the type: a derived manifest version, then every affected item rewritten by the migration steps with a new `row_version` and an audit effect labelled `type_migration`. One transition per collection, all in the one SQLite transaction.
3. Recompute natural keys where the key changed.
4. Re-validate every item against the new version.

If any item fails, or a recomputed natural key collides, the whole save refuses and names the items. It never partially migrates. The caller must have the complete authorized state of every collection of the type (the §7 mutation rule), so a withheld item cannot be silently migrated or revealed. Item identities (`item_key`) never change, so references, including pinned ones, remain valid; a pinned reference keeps pointing at the historical version it named. Views re-render through the projector.

**Declaring a type in conversation.** The flow for "I want to keep my recipes":
1. The agent calls `validate`, then `diff` on its drafted declaration and shows the preview.
2. On confirmation it calls `save-collection-type` with `why`, and optionally `scaffold` to create the first collection at `Knowledge Base/Recipes/_collection.md`.
3. The very next call can add items (14.7). The type's placement is registered as a structured layer, so its views are excluded from ordinary recall like `Records` and `Planning` (`recall_policy`) and are pinned for the hosted gateway (`hosted_gateway`). Both read the type registry instead of hard-coded layer names.

#### 14.6 Default audience and governance

`default_audience` composes with the existing governance kernel and adds no policy language.
Policy stays canonical only in the vault's `_Governance` files. A type declaration therefore never writes or synthesizes a scope. Instead, the type contributes a subject attribute.
- **`owner`** (the default for declared types): every item of the type is evaluated with **subject-level default-deny**. The kernel applies exactly the existing `Scope.default_deny` rule (`governance/decisions.py:350-410`) as if one of the item's scopes set it:
  - an audience that no standing rule names for a scope matching the item receives `DISCLOSURE_MIN`;
  - the owner is never subject to it;
  - explain output reports the source as `default_deny_source: collection-type:<name>` beside `default_deny_scope_ids`.

  Sharing recipes with an audience is therefore an ordinary authored standing rule over a scope that matches them, for example a `paths` selector on `Knowledge Base/Recipes/**` or a `types` selector on `recipe`. No type change is needed.
- **`policy`** (the built-in `records` and `planning`, to keep today's behaviour): no subject-level default; authored policy applies unchanged.

Row-level policy (§7) applies within every type. Tightening `policy` → `owner` is a compatible type change. Widening `owner` → `policy` is classed `release-widening` by `diff` and may be saved only by the owner principal, like the owner-only `schema_memory` operations today (`commands.py:10513-10525`). Withheld remains indistinguishable from absent for every type. The type-registry hash joins the governance compile fingerprint inputs, so a changed default re-derives decisions and never serves a stale one.

#### 14.7 Generic operations; `record_memory` and `plan_memory` are typed facades

The leaf API is generic: `collections.ops.{describe, validate, inspect, create, query, add, update, transition, revise, rebaseline, bulk_upsert, hold, resume, discard}`. It takes a collection and resolves its type. `transition` is the generic lifecycle move: `item_key`, `to_state`, `expected_item_version`, `why`. It is checked against the declared state machine, per-state constraints and named validators.

The two existing tools become **facades**, which are declarative maps held in the built-in declarations' `wire` blocks:
- **`record_memory` → type `records`.** Actions map one to one (`append` → `add`). The receipt, error codes, argument sets and describe text are unchanged.
- **`plan_memory` → type `planning`.** `add` → `add`, `update` → `update`, `triage` → `transition` plus field changes, and `revise`/`rebaseline`/`inspect`/`query` → generic. Receipts, `STALE_PLAN_*` / `PLAN_ID_CONFLICT` codes and the exact inspect shape are unchanged. The Planning hierarchy is the `planning.hierarchy.v1` validator.

The facades refuse a collection of the other built-in type (today's boundary scenarios hold).

**Declared-type items** need an MCP surface (**Needs ruling R8**). Recommendation: `record_memory` also accepts any collection whose type is neither `planning` nor another facade's type. For declared types:
- it uses generic names (`item_key`, receipt marker `_collection_receipt`, generic error codes);
- it gains `action: "transition"`;
- `describe` takes an optional `collection_type` and teaches that type from its declaration.

That adds no tool, and it keeps the frozen hosted candidates unchanged: the new action and parameter appear only on the local surface and the v5 candidate. The alternative is a new generic `collection_memory` tool, with `record_memory` and `plan_memory` as pure facades. That is cleaner naming but one more tool against the fixed tool budget.

#### 14.8 Worked example: Recipes (procedural) and Recipe Executions (observed)

All names and values are invented.

**1. Declare the type.** The user says they want to keep their recipes and log each time they cook one.
- The agent drafts the `recipes` declaration from 14.1.
- It runs `schema_memory(subject="collection-types", operation="diff", proposal=…)`. The result has no findings, the change class is `new`, the placement is `Knowledge Base/Recipes/`, the roles are `[methods]`, and the default audience is `owner`.
- After confirmation it runs `save-collection-type` with `why: "keep canonical recipes"` and `scaffold: {title: "Weeknight recipes"}`.

The result is one transaction holding type version 1, collection `Weeknight recipes`, the implicit scope `collection-type:recipes`, and a manifest view at `Knowledge Base/Recipes/Weeknight recipes/_collection.md`.

**2. Add a recipe and revise it.**
- `add` creates "Weeknight lentil soup": `serves: 4`, `total_minutes: 40`, six steps, status `draft`, row version 1.
- `transition` to `current` makes row version 2 (label: transition).
- Two weeks later the owner edits the view in Obsidian: stir in lemon juice at the end, `total_minutes: 35`. Edit-back applies a governed `update` (actor `owner:view-edit`), which is row version 3 labelled **revision**: "Revision 3, supersedes 2".

The history page shows both. The item view carries the "Revision 3" footer. Version 2 remains readable through a pinned reference.

**3. Log executions in a Records collection.** "Recipe Executions" is an ordinary collection of the built-in `records` type (`kind: observed`):

```yaml
collection_type: records
title: Recipe Executions
item_schema:
  fields:
    recipe:        {type: link, target: {collection_type: recipes}, pin: version, required: true}
    cooked_on:     {type: date, required: true}
    outcome:       {type: enum, values: [great, fine, poor], required: true}
    minutes_taken: {type: integer}
    notes:         {type: string}
  natural_key: [recipe, cooked_on]
```

| cooked_on | recipe (pinned) | outcome | minutes_taken |
| --- | --- | --- | ---: |
| 2026-09-02 | `exomem://recipe/<cid>/<key>@2` | fine | 45 |
| 2026-09-09 | `exomem://recipe/<cid>/<key>@2` | fine | 42 |
| 2026-09-16 | `exomem://recipe/<cid>/<key>@3` | great | 36 |
| 2026-09-23 | `exomem://recipe/<cid>/<key>@3` | great | 34 |

The collection declares a saved view `by-revision` with `aggregate: {op: avg, column: minutes_taken, group: recipe.version}` (grouping is a saved-view feature today). `record_memory(action="query", collection="Recipe Executions", view="by-revision")` returns 43.5 for revision 2 and 35 for revision 3, and a sibling view counts `outcome` per revision the same way. Because each row pins the revision it followed, outcomes compare across versions. A later revision 4 does not rewrite the meaning of past runs. A correction to a run, such as "it was 44 minutes, not 45", is an `update` of that execution row, labelled **correction** because the type is observed.

**4. Surfacing differs by kind.** The storage underneath is the same.
- Turn: *"how do I make the lentil soup again?"* The shipped `methods` cue "how do i" and the type cue "how do i make" both select `methods`. The `match_fields` item anchor resolves "lentil soup" to the recipe. The `collections` lane with `collection_kinds: [procedural]` serves **revision 3 only**, status `current`, within `max_items: 2`. Revision 2 and retired recipes are not served.
- Turn: *"how did the soup turn out last time?"* The cue "last time" plus the resolved recipe anchor selects `recent_change` / `current_state`. The lane with `collection_kinds: [observed]` serves the newest executions linked to that recipe (2026-09-23, great, 34 min), newest first.

**5. What is identical** for the two collections: one store, row versions and generation guards, one transition per mutation on the audit table with a history page, projection and edit-back, natural-key uniqueness (`[title]` for recipes, `[recipe, cooked_on]` for executions), bulk upsert, snapshots and migration. Governance is also the same mechanism: both are row-level subjects of the same evaluator. The recipes carry subject-level default-deny from their type (`owner`), and the executions follow authored policy for `Knowledge Base/Records/…` (`policy`). The only kind-dependent behaviour is the update label (revision versus correction), which version is served, and which roles serve them.

**6. Evolve the type.** Version 2 adds `last_verified: {type: date}`. `diff` says `compatible`, and the save touches no item.

Version 3 renames `total_minutes` to `minutes` and makes `serves` required with `default_value: 2`. `diff` says `migrating` and previews "1 collection, 1 item changes, 0 fail". The save runs in one transaction: type version 3, a manifest version, and the recipe's row version 4 labelled `type_migration`.

The pinned executions still point at revisions 2 and 3 as they were. Grouping by `recipe.version` still works, and the historical values keep their old field name in `item_versions`, reported under the version's own type version.

### 15. Answers to the #1457 cost list

PR #1457 compared its index-backed file design with a throwaway SQLite-authoritative prototype (`scripts/prototype-sqlite-records.py` on `claude/records-write-latency-nv6aui`). Its numbers:
- the prototype appends at about 93/100 ms p50/p95 end to end at both 1,000 and 10,000 items, of which the storage layer is 2.6–3.8 ms;
- the guard refresh costs 0.02–0.04 ms in storage;
- async projection lag was 4–34 ms;
- the file design at 10,000 items is 5.7 s p95.

It recommended not switching authority yet, and listed what a switch costs. The owner has decided the switch. Each cost item is answered here, with where the design handles it.

**1. Migration of existing collections and audit chains; the meaning of client-held hashes.**
- **Item identity.** Rows keep each item's UUID key, natural key, values, body and payload hash (§10 step 2, proof checks a–c).
- **Audit history.** The split history (manifest head, `log.md`, `_archive/logs/`) is parsed by the extracted legacy reader and imported event by event. Each event keeps its verbatim JSON, its transition id and its parent link, chained in `txns` (§10 step 3). Proof check (d) requires the imported head and chain length to equal the legacy ones, and check (f) requires the legacy inspect status to be preserved: a `gap` stays a `gap`, never blessed. The existing gap, continuity and rebaseline semantics therefore still hold for imported history (§8 C6).
- **Client-held hashes.** A client-held `expected_container_hash` changes derivation (§8 C1–C2). It gets exactly one `STALE_RECORD` and refreshes.

**2. Authority split between a Markdown manifest (schema) and the database (rows).** There is no split.
- The manifest text is canonical in the store (`collection_manifests`, versioned with the rows in the same transactions). `_collection.md` is its view.
- A manifest edit in Obsidian becomes a governed `revise` through edit-back, or a held correction (§6).
- Type-level schema lives in `collection_type_versions` (§14.5).

Schema and rows therefore change together, atomically, under one audit chain. The #1457 cost item assumed the manifest stays a user-edited file; this design removes that assumption.

**3. Backup and restore (restic, vault copy).** #1457 is right that a live WAL database copied mid-write can be torn, and that a database in the state root is not captured by copying the vault. The answer is §1 plus §11:
- The live store stays out of the vault.
- A single-file, `integrity_check`ed replica is published into the vault. It is made with the backup API into a staging file and renamed atomically. `VACUUM INTO` is an equivalent primitive and would be acceptable.
- The replica is published after commits (coalesced) and synchronously on quiesce, lease release, shutdown, handoff and export. A restic snapshot or a folder copy of the vault is therefore a complete, consistent backup.
- **A vault copy is not a read-only mirror.** Starting a service on the copy adopts the replica as its live store (same `store_id`, §1 takeover rule), and it is writable from then on.
- **Restore** is: restore the vault, start the service, adopt the replica, re-render views into staging, and compare. A difference becomes a held correction, never an overwrite (§11).
- The exposure is the replica's coalescing window for a crash between commit and publication, bounded at 1 s. It is flushed synchronously at every orderly boundary. Operators who need zero window use `exomem collections backup --stdout` as the restic source.

**4. Obsidian visibility and edits.** Views are not read-only in effect. The owner chose governed edit-back (option 2): a valid edit becomes an audited update, and an invalid, ambiguous or conflicting one becomes a held correction. An edit is never silently overwritten by the next projection, because edit-back runs on the changed file before any re-render, and a stale-base edit is held with the human's bytes (§6).

**5. Sync tools and two machines writing.** This is the one place the store design is genuinely weaker than files. Today two machines writing different items merge through ordinary file sync, and a same-file conflict becomes a sync conflict copy.
- **With the multi-host writer lease** (the supported multi-writer setup), only the lease holder writes the store. Edits made on another machine are view edits. They reach the writer's vault through sync and go through edit-back there, so no store merge is ever needed (§1, §6). Sync conflict copies of views are held `VIEW_CONFLICT_COPY`.
- **Without the lease**, two services each writing their own live store on one synced vault diverge. The takeover check detects it: same `store_id`, and each store has transactions the other lacks. Collection writes then fail closed with `COLLECTION_STORE_DIVERGED`, while reads and knowledge writes continue.
- **Nothing is lost.** An operator command, `maintain_memory(mode="collections-store-reconcile", dry_run=...)`, takes the divergent store's transactions after the fork point. It turns every changed item into a held view correction on the surviving store, carrying that store's values and diagnostics, and records the reconciliation as one content-free transaction. A human then resolves each one, as with a sync conflict copy today.
- Running two writers without the lease becomes explicitly unsupported for collection writes. `describe` and the doctor probe say so. Knowledge Markdown is unaffected.

**6. Out-of-band edit policy.** This is a product decision the file design does not need, and the owner has made it: ingest through the governed write path, or hold (§6). Detection is exact. `projection_state.rendered_sha256` identifies the design's own writes, and a changed file is classified against the row version it was rendered from.

**7. Everything else that reads items as pages; read-your-writes.**
- **Item views are published before the acknowledgement** (§5). Any file reader, including `get_page` and the next agent turn, sees the acknowledged write. That costs about 3 ms, as #1457 measured. Only aggregate views (log layout, history and type pages) and index sync are asynchronous.
- **Structured readers move to the store** (task 5.5): due-state, plan progress, capture sweeps, the working set and current-state resolution, `audit` outcome bindings, and evidence bundles built from items. They are read-your-writes by construction and no longer parse Markdown.
- **`find` and recall** already exclude Records and Planning descendants except manifests (`recall_policy`). Declared types join that exclusion (§14.5). Lexical, resolver and graph indexing of views therefore lagging the drain does not change any recall answer about item values.
- **Plan links, supersession and pinned references** resolve through the store (§14.3), not through view files.

**8. Hosted cells.**
- One writer lease per cell makes SQLite a natural fit, and the live store sits in the cell state root on the tenant volume.
- Cell export and restore gain the store snapshot as canonical data, excluding `-wal`/`-shm` (§11; the `hosted-vault-portability` delta). Restore stages, validates and re-renders before publication.
- `cloud_import` of a file-canonical vault runs the §10 importer and proof in staging before the cell starts.
- Evidence bundles read rows.

**9. Governance and withheld = absent, and per-row cost.**
- Each row keeps its view path as its governance subject, along with its ref, type, tags and project (§7).
- Per-row evaluation is avoided in the common case by the uniform-release decision: one decision when no scope in the resolved policy can distinguish the collection's rows (0.4 ms at 10,000 in the spike).
- When rows are distinguishable, the per-row pass evaluates identity-only columns against a once-resolved policy (44 ms at 10,000 in the spike). Where the distinguishing selectors are path prefixes, refs or declared tags, they compile into a SQL predicate with a tombstone join, and the pass runs in the database. Other selectors fall back to the in-memory pass. Both are held to the parity test.
- Audit rows are redacted by the same subject rules (history pages §5; `inspect` history §7).

**10. Deletion and trash.** Records and Planning have no item deletion today, and this change adds none.
- The file tools (`delete_file`, `move_file`, `delete_directory`, `recover_from_trash`) refuse on paths that `projection_state` owns, with `COLLECTION_VIEW_PATH` and a remediation naming the collection operation.
- If a view is removed outside Exomem, the deletion is held as `VIEW_DELETED` and the view is re-rendered.
- The trash never holds canonical collection data, so trash recovery has nothing to restore for collections.

**11. Exactly-once.** #1457 notes that exactly-once already comes from the idempotency ledger, independent of storage. The store adds the one window the ledger cannot close: a commit before the ledger records it. It does so with `txns.request_id` and the receipt recorded in the same transaction (§4).

**12. Recommendation for the middle path (a derived SQLite sidecar with files authoritative).** It is superseded by the owner's decision. Its revisit conditions are now met, because bulk upsert (#1452) and declarable collection types (§14) make multi-row transactions and larger collections first-class. The sidecar's benefits (O(1) guard, O(1) natural-key lookup) are the store's primary indexes, and the stat-generation item cache that #1457 added (`record_item_cache.py`) becomes unnecessary for store collections. It stays useful for file mode during the legacy window (R7).

## Risks / Trade-offs

- **Opaque storage.** Canonical rows are no longer greppable Markdown. *Mitigation:* views at today's paths, the replica as one portable file, the reverse exporter, and the history page.
- **Views can lag.** A reader of the vault sees a view up to the projector's drain time behind the store. *Mitigation:* the projector is post-commit and immediate (sub-second in the spike), inspect reports pending views, and edit-back refuses to apply an edit made against a stale view (`VIEW_CONFLICT`).
- **Edit-back surprises.** An edit can land as a held correction instead of applying. *Mitigation:* held views are visible under `Held/`, in inspect and in attention. The settle windows avoid holding mid-typing states.
- **SQLite on network filesystems** (SMB/NFS) is unsafe with WAL. *Mitigation:* the live store sits on the state root, which is a local path by default. Readiness verifies `journal_mode=wal` took effect and refuses a network-mounted state root.
- **SQLite version floor** (≥ 3.38 for STRICT and JSON). Python ≥ 3.11 builds bundle newer SQLite on Windows and macOS. Linux distributions vary. Readiness refuses with a clear remediation.
- **Two writers without the lease** can no longer merge collections through file sync. Divergence fails closed and is reconciled into held corrections (§15, item 5).
- **Multi-host RPO** equals the replica coalescing window. That matches today's file replication lag, and divergence fails closed (§1).
- **Two modes during migration.** They add temporary complexity. *Mitigation:* the switch is per vault, and deletion is a scheduled task with its own acceptance.

## Needs ruling

- **R1 Placement.** Recommended: the live store at the state root (`external-canonical`), with a coalesced single-file replica in the vault (`Knowledge Base/_Collections/collections.sqlite`). The alternative, a live WAL store inside the vault, contradicts `machine-local-state-placement` and is unsafe under file sync.
- **R2 Sequencing with #1452.** Recommended: land #1457 fix 1 (policy resolved once per operation) now, because this design reuses it. Re-target #1452's code onto the store instead of building it on files. #1452's request and response contract carries over, minus `BULK_UPSERT_AUDIT_DEPTH`. The alternative, shipping #1452 on files first (its option C), relieves import pain sooner but adds file-audit machinery that §13 then deletes.
- **R3 Row-level audience.** Recommended: express it through existing policy scopes evaluated per row (path, ref, tags), with no per-row audience column.
- **R4 `log.md`.** Recommended: stop writing Records and Planning audit lines into `Knowledge Base/log.md`, and replace them with per-collection history pages (owner addendum). Confirm that no vault-wide one-line digest is wanted.
- **R5 View normalization.** Recommended: re-render normalizes frontmatter formatting, with the body and values exact. This replaces today's byte-preservation of untouched YAML.
- **R6 Datasets.** Recommended: the `dataset` strategy stays file-canonical and query-only.
- **R8 Declared-type item surface.** Recommended: `record_memory` also serves collections of declared types (generic names, new `transition` action), with no new tool and frozen candidates unchanged. The alternative is a new generic `collection_memory` tool, with `record_memory` and `plan_memory` as pure facades.
- **R9 Kind vocabulary.** Recommended first set: `observed`, `intended`, `procedural`, `reference`. Each needs a compiler-role mapping and version wording, so the set grows only by shipped revision. Candidates for later: `constraint` (→ `constraints`) and `precedent` (→ `precedents`).
- **R10 Declared placement.** Recommended: one new top-level Knowledge Base segment per type (`Knowledge Base/Recipes/`), refused if it collides with a reserved layer or a non-empty ordinary directory. The alternative, nesting under a shared `Knowledge Base/Collections/<Type>/`, avoids top-level growth but moves no built-in type.
- **R7 Legacy window.** Recommended: keep file mode and the reverse exporter for two minor releases after the store ships, then run the §13 deletion.
