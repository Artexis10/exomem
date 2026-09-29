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

## Goals / Non-Goals

**Goals**
- One embedded store per vault that is the only source of truth for Records and Planning collections, their manifests, held candidates and audit.
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
- New collection concepts: no delete action, no new item kinds, no new policy language.
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
  profile TEXT NOT NULL CHECK (profile IN ('records','planning')),
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

**The projector is post-commit and crash-safe.**
- A commit marks the affected `projection_state` rows `pending` in the same transaction.
- A single projector task renders them after commit, off the acknowledgement path. It writes a target-adjacent staging file, fsyncs and renames it, then records `rendered_sha256`, `rendered_row_version` and `current`.
- On restart it drains every `pending` row.
- `inspect` reports pending views (Planning: diagnostics code `PROJECTION_PENDING`; Records: an additive `projection` summary).
- Projection failure never fails a committed mutation. It raises attention and retries.

**History page.** It is generated from `txns` and `audit_effects` and readable like today's `log.md` entries: `## <date> <operation>`, then the actor, the reason, and wikilinks to the changed item views, newest first.
- The main page holds the latest 200 transitions. Older transitions go to per-year pages, and only the current year page is rewritten.
- It carries no item values, only content-free facts, as audit events do today.
- It is rendered for the collection-level release decision. Effects on rows that the collection-level audience could not read are omitted, and a transaction touching only such rows is omitted entirely, so the page never discloses more than its own path's release. Full per-row history remains available through `inspect` / `include_agent_history` under per-row authorization.
- Edits to history pages are ignored and the page is re-rendered. It is read-only by contract, and no edit-back exists for audit.

**Indexing.** Views stay ordinary vault files. Recall exclusion is unchanged (`recall_policy.is_recall_candidate` already excludes `Knowledge Base/Records` and `Knowledge Base/Planning` descendants except manifests). The lexical, resolver and graph sync of a view happens when the projector publishes it, so it is no longer on the mutation's acknowledgement path.

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
- `ref`: `exomem://record/<cid>/<key>` or `exomem://plan/<cid>/<key>`;
- `type`: `record` or `plan`;
- `tags`: the values of a schema-declared `tags` field, if any;
- `project`: the project of the manifest.

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
| view render + publish | off the ack path | inside the write | 1.2 ms p95 |

The spike numbers come from a 4-core container with ext4, SQLite 3.45.1, WAL and `synchronous=FULL` (`benchmarks/collections_sqlite_spike/results-*.json`). They exclude the dispatcher, the idempotency ledger, governance resolution and receipt projection.

**End-to-end append budget.** From #1457: the dispatcher and ledger are ≈ 4 ms and flat. Policy resolved once per operation is a few ms. Manifest parsing (≈ 80 ms today, 9 parses) becomes a parse cached per `manifest_version`. The store transaction is < 1 ms. Receipt projection is ≈ 1 ms.

Removed from the acknowledgement path:
- the full collection read (≈ 4.3 ms per item);
- about 20 directory censuses;
- `log.md` read-rewrite;
- synchronous view index sync (≈ 86 ms), which moves to the projector.

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

**Related specs**
- The `structured-collections` requirements whose subject is the file audit protocol (marker, head, activity-log event) are rewritten by the deltas in this change.

`dataset` adapters, knowledge-note writers, `batch_atomic_write`, the governance evaluator and the query evaluator are not deleted. The size of the deletion is measured in the deletion task, not estimated here.

## Risks / Trade-offs

- **Opaque storage.** Canonical rows are no longer greppable Markdown. *Mitigation:* views at today's paths, the replica as one portable file, the reverse exporter, and the history page.
- **Views can lag.** A reader of the vault sees a view up to the projector's drain time behind the store. *Mitigation:* the projector is post-commit and immediate (sub-second in the spike), inspect reports pending views, and edit-back refuses to apply an edit made against a stale view (`VIEW_CONFLICT`).
- **Edit-back surprises.** An edit can land as a held correction instead of applying. *Mitigation:* held views are visible under `Held/`, in inspect and in attention. The settle windows avoid holding mid-typing states.
- **SQLite on network filesystems** (SMB/NFS) is unsafe with WAL. *Mitigation:* the live store sits on the state root, which is a local path by default. Readiness verifies `journal_mode=wal` took effect and refuses a network-mounted state root.
- **SQLite version floor** (≥ 3.38 for STRICT and JSON). Python ≥ 3.11 builds bundle newer SQLite on Windows and macOS. Linux distributions vary. Readiness refuses with a clear remediation.
- **Multi-host RPO** equals the replica coalescing window. That matches today's file replication lag, and divergence fails closed (§1).
- **Two modes during migration.** They add temporary complexity. *Mitigation:* the switch is per vault, and deletion is a scheduled task with its own acceptance.

## Needs ruling

- **R1 Placement.** Recommended: the live store at the state root (`external-canonical`), with a coalesced single-file replica in the vault (`Knowledge Base/_Collections/collections.sqlite`). The alternative, a live WAL store inside the vault, contradicts `machine-local-state-placement` and is unsafe under file sync.
- **R2 Sequencing with #1452.** Recommended: land #1457 fix 1 (policy resolved once per operation) now, because this design reuses it. Re-target #1452's code onto the store instead of building it on files. #1452's request and response contract carries over, minus `BULK_UPSERT_AUDIT_DEPTH`. The alternative, shipping #1452 on files first (its option C), relieves import pain sooner but adds file-audit machinery that §13 then deletes.
- **R3 Row-level audience.** Recommended: express it through existing policy scopes evaluated per row (path, ref, tags), with no per-row audience column.
- **R4 `log.md`.** Recommended: stop writing Records and Planning audit lines into `Knowledge Base/log.md`, and replace them with per-collection history pages (owner addendum). Confirm that no vault-wide one-line digest is wanted.
- **R5 View normalization.** Recommended: re-render normalizes frontmatter formatting, with the body and values exact. This replaces today's byte-preservation of untouched YAML.
- **R6 Datasets.** Recommended: the `dataset` strategy stays file-canonical and query-only.
- **R7 Legacy window.** Recommended: keep file mode and the reverse exporter for two minor releases after the store ships, then run the §13 deletion.
