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
- Unchanged external tool contracts wherever possible, with every change listed (§8). Hosted profiles are retired.
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
- Reviving retired Hosted profiles.
- Server-side interpretation of edited prose. Edit-back is deterministic parsing and validation only (pure substrate).

## Decisions

### 0. GA uses one hosting node with backups

One hosting node owns the live store for this GA. Backups, explicit restore, local restart and managed upgrade handoff remain required. The existing lease, schema compatibility and single-writer boundaries still apply.

Cross-host takeover, replica transport, fleet custody and divergent-host reconciliation are deferred until a concrete use case can exercise them. Their protocols below describe a future deployment scope; they do not block single-host GA. No file-sync product is required for the single-host deployment.

The in-vault snapshot remains a backup and portability artifact. Its existing publication costs and applicable resource limits remain measured obligations. This scope does not authorize removal of publication or lifecycle guarantees.

GA retains the current explicit per-collection view modes. Editable Markdown and bounded overviews remain supported without automatic size-based conversion. Existing populated-mode conversion limits remain in force. A human table editor and selective item publication are deferred beyond GA.

### 1. One store per vault, and where it lives

The live store is one SQLite file per vault: `collections.sqlite` under the vault's state root (`state_paths.vault_state_dir`), with the pragmas `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON` and `busy_timeout`. It is classified with a new placement class, `external-canonical`. Like `external-state`, it lives outside the vault so no file-sync agent ever sees a live WAL database. Unlike `external-state`, it is canonical:
- it is never rebuilt from other state and never deleted by index maintenance or rebuild;
- migration moves it and never drops it;
- backups and portability include it.

The vault carries a **replica**: `Knowledge Base/_Collections/collections.sqlite`. It is a single-file (`journal_mode=DELETE`) consistent snapshot, published by the backup API to a target-adjacent staging file, checked with `PRAGMA quick_check`, and atomically renamed into place.
- **When it is published:** after committed transactions, coalesced to at most one publish per 60 s under steady writes (A9). The snapshot copy runs outside the vault write boundary, so writes proceed during it; two brief boundary steps (record the intent, then verify and swap) delay a write that arrives during one, in proportion to the replica's size. Publication is check-then-swap against the last replica this instance published (A4). It is always flushed synchronously on quiesce, writer-lease release, shutdown, upgrade handoff and portability export.
- **What it carries:** in `store_meta`, the `store_id`, the publishing `instance_id` and its lineage, a store-wide `commit_seq` and chained `store_head_hash` (A3, A4), and the lease epoch. Explicit restore binds the snapshot to trusted enrollment and the current fence; the mode marker is a compatibility hint (A5).
- **How it is used:** it is what restic, vault copies and multi-host replicas carry. It is never opened for writing in place.

Single source of truth is preserved because exactly one live store accepts writes: the one held by the writer-lease holder. The replica is a published copy of it, in the same sense that the Markdown views are.

**Multi-host takeover** (opt-in `multi-host-writer-lease`) is specified in §16 A3 and A4, which supersede the earlier replica-only rule:
- The lease holder reports its store head `(store_id, instance_id, commit_seq, head_hash)` to the coordinator on every renew and release.
- A new holder facing a head recorded by another instance adopts the replica only once the replica reaches that head. Until then it refuses collection writes with the retryable `COLLECTION_STORE_SYNC_PENDING`.
- A single-host store, where no foreign head was ever recorded, never waits.
- `exomem collections adopt-local` continues deliberately from local state, recording the fork point.
- Any foreign replica or view stamp is `COLLECTION_STORE_DIVERGED` immediately. Collection writes refuse, and knowledge writes and all reads continue.
- The crash recovery point is the replica publish interval. Stranded transactions stay in the crashed host's store and are reconciled into held corrections (§15 item 5).

*Alternative rejected:* the live store inside the vault. The ratified `machine-local-state-placement` requirement exists because sync agents hash, hold and replace database files, and a WAL database copied mid-checkpoint is corrupt. Ruled (R1): the live store under the per-vault state root, plus the integrity-checked single-file replica at `Knowledge Base/_Collections/collections.sqlite`.

### 2. Schema

These are STRICT tables. `collection_store/schema.py` owns the DDL and migrations, with `store_meta.schema_version` starting at 1. The spike (`benchmarks/collections_sqlite_spike/spike.py`) implements the item, version, source, transaction and effect core of it.

```sql
store_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)
  -- schema_version, store_id (uuid4, the store lineage), instance_id (this physical copy, A4),
  -- lineage (JSON: instance_id, adopted_from, adopted_at_commit_seq, head_hash), forks (A3),
  -- commit_seq, store_head_hash (A3), last_published_replica_sha256 (A4), created_at,
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
  verified_through_txn INTEGER,              -- audit chain verified up to here (A6)
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
  commit_seq INTEGER NOT NULL UNIQUE,        -- store-wide sequence (A3)
  store_head_hash TEXT NOT NULL UNIQUE,      -- store-wide chained head (A3)
  legacy_event_json TEXT)                    -- verbatim imported audit-v1/v2 event, else NULL

audit_effects(                               -- one row per changed item in a transaction
  txn_id, ordinal, row_id, item_key, effect TEXT CHECK (effect IN ('insert','update','held','resume')),
  effect_label TEXT,                         -- kind-dependent: correction|revision|replan|edit|transition|type_migration|view_move
  version_before, version_after, hash_before, hash_after, source_ref,
  PRIMARY KEY (txn_id, ordinal))

held_candidates(held_id TEXT PRIMARY KEY, collection_id, kind TEXT CHECK (kind IN
  ('write-refusal','view-correction')), code TEXT,  -- VIEW_CONFLICT|VIEW_INVALID|VIEW_FOREIGN|VIEW_MOVED|...
  candidate_json, held_bytes BLOB, diagnostics_json, view_path,
  base_row_version, created_txn, updated_at)

projection_state(path TEXT PRIMARY KEY, collection_id, row_id, kind TEXT CHECK (kind IN
  ('manifest','item','log','held','history','type')),
  published_row_version INTEGER, published_sha256 TEXT,   -- last install known on disk
  pending_row_version INTEGER, pending_sha256 TEXT,       -- staged in the main transaction (A7)
  stat_identity TEXT,                                     -- (inode, size, mtime_ns) fast path (A1)
  state TEXT CHECK (state IN ('current','pending','held')))
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

**Planning compatibility ruling (2026-09-30, F10; clarified 2026-10-01).** Store-mode public Planning `add` with an explicit existing `plan_id` or the same identity derived from a complete natural key, an identical normalized payload and a proven original insert returns the ordinary valid Planning receipt with `outcome: "replayed"`, without a new item, generation or transition. Derived-identity replay already follows the canonical structured-collections idempotency requirement; it is not a new capability. Root independently reproduced both `[title]` and `[title, kind]` retries: legacy file mode returns `PLAN_ID_CONFLICT`, while store mode replays without changing its complete dump. The store corrects that legacy refusal with scaffolding either enabled or disabled. File mode is unchanged. This is the only error-parity exception for those exact retries; changed content, a natural-key twin with a different identity, invalid arguments and stale guards retain their ruled refusals. The public golden records each mode's exact outcome rather than normalizing a refusal into a receipt.

**Inspection compatibility ruling (2026-10-01).** Records targeted inspection remains a generic collection inspection: it can inspect an existing Planning collection. Planning inspection still requires the Planning profile. Store preview must preserve that existing asymmetry, including successful UUID and manifest-path selectors and unchanged refusal codes for invalid or missing selectors. Inspection never mutates the store.

View edits use a deterministic identity, `sha256(view_path, base_row_version, sha256(file bytes))`, so a watcher replay after a crash is a no-op (§6).

**Historical curation receipts.** A file-mode Records step committed before migration keeps its existing witness. New import checkpoints bind each imported item file to its exact captured source SHA-256 and structural audit marker in `original_items`. This mapping lives in the existing immutable checkpoint envelope and its event hash; it adds no table or schema migration. A missing or mismatched marker remains evidence of the original state, without repairing a gap.

Verification authorizes the current collection before reading this proof. It joins the checkpoint, imported event and unchanged canonical item version to the sealed destination and witness hash. Append also matches the sealed payload; update also matches every sealed preimage guard. A later edit and revert cannot pass because the imported version changed. Regenerated Markdown formatting does not affect this proof.

Historical verification never recovers a missing witness, supplies a request identity or decides whether to repeat a write. Native transactions, including file-sealed steps committed after migration, retain their existing request-row proof. Older checkpoints without the source binding remain uncertain unless existing evidence proves the postcondition; normalized rows cannot reconstruct original byte hashes.

### 5. Markdown projection

Every canonical object has a rendered view at today's path:

| View | Path | Edit-back |
| --- | --- | --- |
| Manifest | `<collection>/_collection.md` | yes, as a governed `revise` (§6) |
| Item (items layout) | `item_filename` recipe path, else `<source>/<item_key>.md` | yes, as a governed `update` (Planning: `update`, or `triage` for triage-only fields) |
| Log (log layout) | the declared log file | yes, per block: a changed block is an update; a new or removed block is held |
| Held candidate | `<collection>/Held/<held_id>.md` | no; read-only, resumed through the tool |
| History | `<collection>/_history.md` (newest), plus `<collection>/_history/<NNNN>.md` pages of 500 transitions (A6) | no; read-only, rewritten if touched |

**Log frame.** For the log layout, the prose outside the declared item section (headings, legend, notation), the BOM, the newline style and the final-newline state are stored in `collections.log_frame_json` and re-emitted byte-identically. An edit to the frame is recorded as a content-free `view_frame_edit` transaction, and it never touches items.
- The frame has one versioned shape, version 1: `text` (everything before the first block, BOM included), `suffix` (everything from the end of the item section), `newline`, `bom` and `final_newline`. The importer captures it from the file, and a create stores the empty frame: the section heading with LF newlines. Stores created before the shape was versioned hold `{"text"}` alone; the reader treats it as that text with LF newlines and no suffix. No released store holds that shape, so no store migration rewrites it.
- The import refuses with check (b) when a legacy block has a line that none of its row's values holds: a line that is not the block's heading, a child row the adapter parsed into the row, a record-id, audit or view-stamp comment, or blank (`log_view.stray_lines`). The store keeps a block's values only, so such text would be lost at the first render or at an export. Values are compared, not spacing, so a hand-spaced child row imports. The refusal names the block's item key and line.
- One renderer, `collection_store.log_view.render`, writes `text`, the row blocks in insertion order, and `suffix`. Blocks use the frame's newline style. When blocks reach EOF, the renderer respects `final_newline`; a nonempty suffix stays verbatim. An empty heading without a terminal newline gains the separator that its first block needs. The projector adds managed stamps; the export omits them. Neither path removes authored suffix whitespace.
- An append or update of a log row records only the log view's pending basis (`pending_row_version`, `pending_sha256` NULL), as summary pages do, and its receipt carries `projection_pending`. Nothing renders the log on the acknowledgement path.
- A log view's render context, the governance that its holds take, is its collection's current manifest, as for a summary page. Any bytes the store recorded for the log view have that context, the imported file's bytes included, so displaced log bytes can always be held.
- A managed frame stamp binds the store, instance, rendered generation and full canonical frame hash. It lives at a parser-confirmed section boundary, outside authored frame bytes. Existing installation render contexts retain that hash with their generation and digest. Current manifest metadata may establish hold governance but cannot manufacture historical frame proof.
- The decoder accepts one complete managed line outside fences in the frame prefix or declared item section, never a later section. Fenced markers remain authored text. The renderer normally emits the stamp after the declared heading. For an empty EOF heading without a terminator, it emits the stamp before that heading. Removing only the marker line then restores exact authored bytes without guessing whether a separator belongs to the owner.
- Frame comparison uses the canonical renderer's frame representation for the current row occupancy, under the same snapshot. A required first-block separator alone is not a frame edit. A real edit retains its captured proposal bytes and the stored frame hash remains its authenticated base. An owner-added newline in an empty log remains authored content.
- A settled frame edit uses an internal `view_frame_edit` leaf under `owner:view-edit`. It changes only `log_frame_json`, advances the collection generation and queues the log view. It creates no item version, item effect or Planning profile operation. A private frame receipt uses a closed validator; no public action changes. Replay identity binds the path, authenticated frame base and proposed frame hash.
- A frame applies only when its recorded base hash still equals the canonical frame hash. Unrelated row commits do not stale that base. Missing proof or a newer frame preserves the whole edited buffer as a correction. Existing exact unstamped renders keep their compatibility behavior.
- A persisted context for the same path, collection and log kind may prove that hash at a later rendered generation. Require `1 <= stamp generation <= context generation <= current collection generation`, with integer generations. This proves the base content was rendered, without claiming its exact historical generation. A frame change followed by an exact revert permits an unapplied edit of that base. Row stamps and held-byte source-pair governance keep their existing checks.
- A mixed buffer uses separate frame and ordinary row commits. No component commit accepts the whole buffer digest. Publication accepts it only after every component is canonical or durably held. A crash between components leaves the original buffer for deterministic replay and preserves unapplied edits.

**Rendering** reuses today's renderers: `render_markdown_item`, `render_markdown_log_item`, the managed presentation blocks and the filename recipes. The system frontmatter stays (`type`, `collection_id`, `record_id` / `plan_id`, `schema_version`), so identity is visible in the file. Every item and manifest view also carries the **view stamp** `exomem_view: {s, i, v, h}` (store id, instance id, row version, payload-hash prefix), and every log block carries the same stamp as a comment (§16 A2). The audit marker comments and the manifest `record_audit` / `plan_audit` mappings are no longer rendered.

**Views are normalized on re-render.** Frontmatter is emitted in canonical order and style. The authored body is canonical data (`items.body`) and is re-emitted exactly. Only YAML formatting that is not data (quoting style, key order, comments inside frontmatter) is not preserved. This replaces the byte-preservation contract of today's update splicer (ruled R5).

**SQLite commit is the save boundary; Markdown publication follows asynchronously.** The canonical transaction records each changed item or manifest's exact render hash, row version and retained installation context. The writer acknowledges that durable commit without staging or installing those derived files. Rendering CPU and preflight input custody remain on the acknowledgement path. The existing projector stages and publishes pending output through the same check-then-swap protocol (§16 A1).
- Keep `_pending` and its exact hashes, row identities and historical render contexts. Do not generalize the aggregate-only `_pending_basis` for this change. Repeated pending writes retain the context needed to classify an edit against the last physical render. Confirm and retain an installed but unpromoted physical predecessor before filtering contexts or replacing its descriptor. A later queued render must not discard that predecessor's policy context.
- Installation metadata separates `source_pairs` from the latest queued render. Reconcile records `install_ready` after staging and flushing, before commit. Publication requires that committed readiness, token, render context and current pending pair agree. A missing stage does not prove that installation never happened. Recovery uses each old token's own evidence even when SQL pending advances.
- An unresolved input keeps its token, render context and readiness unchanged. Target candidates include ready output; displaced asides retain only their installation's predecessor candidates. Only a guarded physical observation can narrow candidates. A complete candidate set with one policy permits an ordinary historical hold. Missing or conflicting authority uses the existing owner-only quarantine. An editable stamp never selects policy.
- Schema 13 records this metadata compatibility boundary without adding tables or columns. Existing schema guards publish `collections-store-schema-13` before migration. Older schema 12 writers must refuse because they can discard historical policy context. The ordinary lease handoff and source-dark rollout remain required.
- Preserve `capture_previous` and required preservation before canonical writes. Held-view publication keeps its existing synchronous behavior. Background scheduling never discards unseen bytes or weakens held-file checks.
- A crash before commit exposes no proposed item or manifest. A crash after commit leaves durable pending work that the existing startup reconcile can publish. A view is never ahead of the store.
- A blocked or failed publication leaves the mutation committed and the view pending. Inspection reports that state; existing receipt warnings remain available where the receipt envelope supports them. Closed lifecycle receipts gain no new field.
- Log views, history pages and type views use the same asynchronous projector. Startup and periodic reconciliation classify edits before publishing. Quiesce drains recoverable pending work within its existing cooperative budget.
- Structured queries read the committed store immediately. `get_page` keeps its current physical-file read and governed render validation. Missing, stale, held, edited, detached, ambiguous, unknown and unsafe views remain unavailable until valid publication or correction. It never substitutes a canonical render for unknown file state. O2 keeps invalid edited bytes in place.
- File reads retain their current byte, hash and time semantics. Ordinary scripts, `get_page` and Obsidian can observe publication lag. Measure acknowledgement latency and healthy-writer publication lag separately; no numeric lag maximum is established yet.
- **Index sync of views is never on the acknowledgement path.** Lexical, resolver, memory-refs and graph sync of a published view goes to the derived drain. Records and Planning views are already excluded from recall, so a following `find` is unaffected. Structured readers (due-state, plan progress, capture sweeps, the working set, evidence bundles) read the store directly, so they have read-your-writes by construction (§15, item 7).
- `inspect` reports pending views (Planning: diagnostics code `PROJECTION_PENDING`; Records: an additive `projection` summary). Projection failure never fails a committed mutation. It raises attention and retries.

**History page.** It is generated from `txns` and `audit_effects` and readable like today's `log.md` entries: `## <date> <operation>`, then the actor, the reason, and wikilinks to the changed item views, newest first.
- The main page holds the latest 200 transitions. Older transitions go to numbered pages of 500 transitions each. Ordinary append pagination rewrites only the main page and newest numbered page (A6). Governance changes and repair of edited generated pages may regenerate older pages. Their current audience intersection remains mandatory.
- It carries no item values, only content-free facts, as audit events do today.
- It is rendered for the **intersection** of the audiences that can read the page's path (A8). An effect appears only if every such audience can read its item. A transaction touching only omitted effects is omitted entirely, and a mixed bulk transaction with any omitted effect is shown without its `why`. So the page never discloses more than its own path's release. Full per-row history remains available through `inspect` / `include_agent_history` under per-row authorization.
- Edits to history pages are ignored and the page is re-rendered. It is read-only by contract, and no edit-back exists for audit.

**Indexing.** Views stay ordinary vault files. Recall exclusion is unchanged (`recall_policy.is_recall_candidate` already excludes `Knowledge Base/Records` and `Knowledge Base/Planning` descendants except manifests). The lexical, resolver and graph sync of a published view goes to the derived drain, so it is not on the mutation's acknowledgement path.

### 6. Edit-back: the file watcher turns view edits into governed updates

`file_watcher` gains one hook: a changed path that `projection_state` owns goes to `collection_store.edit_back` instead of only the index publishers. Only the writer-lease holder applies edit-back. On other hosts the edit reaches the writer through vault replication and is handled there.

**Detection does not depend on the watcher.** The watcher hook is the fast path. The A1 reconcile (on start, then every 60 s, stat-identity pre-check) is the guaranteed path, and every publish is check-then-swap. So an edit is never lost even when `watchdog` is absent, the service is offline, or an agent write lands inside a settle window. The serving store thread runs the reconcile while the release enables the `collection-views-v1` fence, unless the vault's rollback disabled it; the P2.8 gate adds the fence to the release. Until then, a store classifies view inputs as it did before edit-back, and an owner's edit waits on disk. The reconcile skips every collection whose own route fence is closed, such as a summary collection while `records-summary-v1` is off. It stats and reads the views outside the write transaction, and it runs before the pending-view publication pass, which leaves an edit at an item or manifest view path to edit-back. A formatting-only manifest edit is compared as parsed properties, so reordering them commits nothing.

**Settling.** Editors autosave every keystroke burst. Edit-back acts on a path once it has been quiet for 2 s. A parse or validation failure is held only once the file has stayed invalid for 10 s. Both windows use monotonic time, because a wall clock can step backwards.

**Watcher and census implementation.** The watcher sends path and move hints to an existing store server without waiting or opening SQLite. One bounded mailbox retains the hints; overflow requests reconciliation. The store thread checks exact projection ownership, applies the existing route and lease fences, and uses the existing edit-back leaves. Due notification and settling work receives a bounded turn between requests. Directory events request a census instead of expanding descendants on the watcher thread.

The census scales with materialized files, not canonical rows. Summary collections retain bounded read-only pages and never produce or enumerate per-row views. An item source root admits detached candidates; a log owns its declared file and parsed blocks. Generated held, history and publication files are not new item candidates. Sync-provider filename patterns come from the provider registry and supply classification evidence, never mutation authority.

The served handle owns a paged census cursor. Disposable disk-backed SQLite TEMP metadata retains directory and candidate evidence when exact matching exceeds the memory budget. It creates no canonical census tables and resets on handle replacement or takeover. A pass discovers possible destinations before treating missing paths as deletions. A complete pass reaches every directory's EOF and reads each candidate without unresolved filesystem, identity or size failures. A partial or failed pass preserves files and retries without claiming completeness.

Each window releases its read snapshot before filesystem reads and retains metadata instead of file bytes between turns. It uses the existing held filesystem for traversal and verifies bytes again before mutation. Every pass enumerates its directories; directory identity and timestamps cannot prove unchanged membership or detect a child's changed stamp.

**Census slices.** Between requests, the store thread runs edit-back windows in slices. After its first window, one checkout runs further windows for up to 50 ms (`VIEW_SLICE_SECONDS`). It stops at the first window boundary where a request is queued, a lease handoff drains the store, a writer waits for the boundary, or another request runs. Each window still authorizes its own writes and proves ownership once, for all of its collections together. A slice shares its checkout's admission, and a views tick shares one view of path placement and governance policy, opened per tick; its store writes read both live. Before slices, every 16-path window paid its own checkout, governance policy load and release checks, and on a 10,000-row store at rest that fixed cost kept the store thread in views ticks for about half of the time (tasks P2.8).

**When a tick runs.** The store thread starts a maintenance tick only after foreground work has been quiet for 20 ms (`FOREGROUND_GRACE_SECONDS`): no call in flight, none waiting on a boundary, and none finished within the gap. Back-to-back reads therefore never wait behind a tick, and a client that polls every 50 ms still leaves a tick room between its calls. A views tick that starts after a quiet gap returns before its first publication path when a request waits. Its pass then stays owed, and so does the pass that a maintenance wake starts. An owed pass counts as view work, and the tick keeps its age, so the bounds below count from when the work fell due. The work is deferred and never dropped, and check-then-swap keeps an edit that the pass has not yet seen safe. The settle and invalid-hold windows keep their lengths.

**Starvation bounds.** A tick with work due that has waited 2 s (`MAINTENANCE_DEFER_SECONDS`) for a quiet gap runs one bounded turn regardless. A views turn then runs a single window and publishes a single path. View work is a settled edit, a census window, a notified path or an owed publication pass. A views tick with no view work due only checks the release inputs of history pages; it waits 10 s (`VIEWS_POLL_DEFER_SECONDS`), so that check keeps its bound without a turn every 2 s beside reads. While store requests stay queued without a break, only the views turn after a request runs, so view maintenance gets one turn per bound: a census pass stretches, and a settled view edit applies in seconds instead of 2-3 s. The create, import, backfill and derived ticks have no bound under that demand: as before this gate, they wait for an instant with no request queued. Under other continuous demand, such as a held foreground mark or writers at the boundary, any starved tick runs unless a lease handoff drains the store.

**History pages in the census.** The census checks a history page like every other view: one stat against the stat identity recorded with its own bytes, and a read compared with the recorded published or pending digest only when the stat differs. It never renders a page. A page that differs goes to reconcile, which renders it under the current release by check-then-swap. Freshness comes from the writer's pending marks and from the views tick, which marks every history page pending when `history.release_inputs` changes: the governance policy fingerprint, the access fingerprint, the access blocked flag or the tombstone set. That tick runs on every release that publishes views, so a change to `_access.yaml` or a tombstone reaches the pages on disk within one tick. A change that withholds a row of a collection is the exception: reconcile refuses that collection, and its pages stay pending while the other collections publish. Egress still renders a page before it serves it.

A successful census proves which candidates it observed, not an atomic filesystem snapshot. Duplicate observed candidates remain ambiguous. An in-root move requires one observed destination and rechecks the current collection basis, old-path absence, destination custody, item identity, stamp and bytes. Its transaction changes canonical and projection paths, reauthorizes the destination, and records `view_move` without changing the payload version. Publication keeps its no-clobber and aside rules; a duplicate created after observation becomes a correction on the next pass.

The move retires or safely rebinds installation descriptors and staging so recovery cannot recreate the old path. It preserves canonical render hashes. A simultaneous content edit stays in quarantine while the writer publishes canonical content at the adopted destination. Replay identity includes the observed collection generation, so repeated moves through the same paths remain distinct.

**Detached correction authority.** Candidate frontmatter and copied stamps never establish release authority. A proven row correction retains canonical row and field admission. A detached candidate without proven canonical provenance carries a schema-defined quarantine boolean, separate from its content and canonical metadata. The shared held-subject decision releases quarantined bytes and diagnostics only to the unrestricted owner; standing or session grants cannot bypass that floor. The authorization basis and cache invalidation include the boolean.

Complete equality with the current canonical render establishes detached provenance. The writer compares full bytes or a full digest, never the stamp's truncated hash. A persisted device/inode pair is a change hint, not historical object continuity across closed handles or restart. Move hints, source absence and a unique observed destination also cannot clear quarantine. Target hints may add restrictions but cannot remove quarantine. Legitimate differing detached edits remain visible to the unrestricted owner for ordinary resume.

If the old parent is missing, cleanup may search the freshly held authorized destination parent for exact old token-derived leaves. That search grants no release authority. Cleanup durably quarantines unknown bytes and rechecks current handle version and digest before deletion. A hold inside an open SQL mutation is not durable until COMMIT. A two-link stage enters a post-COMMIT cleanup queue. Cleanup simultaneously holds its target and proves the same current object, versions and digest before removing the redundant alias. Any incomplete open, preservation or unlink leaves recovery pending.

A differing move sets unresolved target provenance in its pending installation descriptor. That protocol state survives new saves, restart, hold discard and descriptor replacement. Every differing target or displaced input remains quarantined until a fresh held target proves a committed canonical render. Existing confirmation transactions clear the current target state; acknowledgement adds no second commit. Preserved descriptors keep their own unresolved state, so current confirmation cannot release unknown old asides. Accepted hashes permit displacement only after their held bytes commit; they never grant release authority.

Explicit resume of an unbound item adopts its existing source filename through the normal append or add leaf. The writer rechecks source-root membership, unchanged held bytes, occupancy and destination authorization before adoption. This preserves the owner's filename as A10 does for moves, until explicit representation maintenance applies a filename recipe. Ordinary appends retain their existing filename rules. A changed source remains untouched and makes the held adoption stale.

Schema 12 adds that boolean to held candidates; existing rows retain their existing authority. The existing schema enrollment, fresh head checks, replica admission and restore paths exclude older readers. This is a persisted authorization boundary, not census cache state. Writer admission upgrades an older store even while `collection-views-v1` remains dark, so that upgrade blocks rollback to schema-11 releases. Release evidence must state the boundary and reuse the existing borrower-drain and schema-refusal proof.

Census windows do not bound publication retirement. That existing seam decodes and visits every retained installation token for one projection. Paging those descriptors remains capacity debt because it needs a recovery continuation contract. An unowned stage or aside may be removed only when its bytes match a committed render or a complete durable held copy. Unknown bytes stay in place and make cleanup pending, including after a move drops the old token's ownership.

**Hold identity.** A held correction's id is UUID-shaped, like every held reference (C10).
- A hold of bytes still at the view path (`VIEW_INVALID` kept in place, `VIEW_DELETED`) is keyed by `(view_path, base_row_version)`. A later save at that base replaces it in place, and a later valid edit of the view resolves it.
- A hold of displaced bytes, which an install moved aside or a re-render will move aside (`VIEW_CONFLICT`, `VIEW_FOREIGN`), keeps content identity: `(view_path, bytes)`. The same bytes displaced twice are held once, and a displaced edit whose bytes a hold at the view path already preserves adds no second hold.
- Replacing or deduplicating a hold never drops displaced bytes. A publication may remove displaced bytes only when the store adopted them or a hold preserves them; the install descriptor records their digests as accepted.

**Classification**, in order, for an item view. The **stamp rules of §16 A2 run first**: a foreign stamp is divergence, an older stamp is `VIEW_CONFLICT`, a hash-mismatched stamp is `VIEW_INVALID`, and only imported legacy bytes may be unstamped. The rows below then apply to views whose stamp names the current row version.

| Observation | Result |
| --- | --- |
| `sha256(bytes)` equals `published_sha256` or `pending_sha256` | own write or no change; nothing happens |
| parses to the same values and body as the row | formatting-only edit; re-render, no transaction |
| stamp `v` older than `items.row_version` (a stale buffer, a second device, or a pending projection) | held `VIEW_CONFLICT` with both versions; the view is re-rendered to current and the human's bytes are kept in the held correction |
| parse failure, undeclared field, type or enum failure, system-field change (`collection_id`, id, `schema_version`), natural-key conflict, Planning lifecycle or hierarchy violation | held `VIEW_INVALID` with the field-addressed diagnostics the tool would return. Classification validates with the hierarchy graph and the stored row, as the leaf does, and any refusal of the leaf is held the same way. **O2:** the edited bytes stay at the view path until a valid edit or explicit discard resolves the correction; until then `get_page` answers that path as absent for every caller, because its bytes are not the current render |
| valid change to declared values and/or body, current base version | ONE governed `update` (Planning: `update`; `triage` when only triage fields changed) through the ordinary writer leaf on the store thread under owner scope: same validation, governance precommit, receipt; recorded as a `view_edit` transition (the Planning `profile_operation` keeps `plan_update` or `plan_triage`), actor `owner:view-edit`, `why: "edited view <path>"`, request identity per §4; then re-render |
| file deleted | held `VIEW_DELETED`; the view is re-rendered, because no delete exists |
| file moved or renamed within the collection's source root, current stamp intact | governed `view_move` (§16 A10): `view_path` updated and audited, no re-render at the old path |
| file moved out of the root or into another collection | held `VIEW_MOVED` with its bytes; the canonical path is re-rendered. The moved file stays where the owner put it and follows ordinary file permissions at its destination. A proven held copy retains canonical row governance; unproven bytes remain quarantined. Without move evidence, an out-of-root move is seen as a deletion (`VIEW_DELETED`), which loses nothing |
| unbound `.md` file created under a projection root | held `VIEW_UNBOUND` as a proposed insert, resumable with `append(held=...)` for Records or `add` for Planning |
| sync-conflict copy (`*.sync-conflict-*`, `* (conflicted copy)*`) | held `VIEW_CONFLICT_COPY`; never applied |

A **manifest view** edit becomes a governed `revise` when it validates as a revision of the current `manifest_version`. Otherwise it is held, with the same classification. Schema-breaking revisions follow today's revise rules. A **log view** is diffed block by block using each block's stamp (§16 A2) and the `exomem-record-id` binding. A changed block is an update. A block with no binding, or a removed block, is held. Reordering is ignored and re-rendered.

**Log view classification (S2).** The whole view is classified first:

| Whole log view | Result |
| --- | --- |
| does not parse as the declared log, or two blocks carry one identity | held `VIEW_INVALID` with the adapter's diagnostics |
| its frame changed but has no authentic current frame base | held `VIEW_INVALID`, issue `VIEW_FRAME_CHANGED` |
| a block carries another store's or instance's stamp | held `VIEW_FOREIGN`; `COLLECTION_STORE_DIVERGED` |
| no block carries a stamp, and the view shows stored rows | held `VIEW_INVALID`, issue `VIEW_UNSTAMPED`, whose remedy says to make the edit again in the re-rendered, stamped view and then discard the correction |

Otherwise each block is classified against its own row:

| Block | Result |
| --- | --- |
| no `exomem-record-id`, or one that names no row of the collection | held `VIEW_UNBOUND` |
| no stamp, while other blocks carry one | held `VIEW_INVALID`, issue `VIEW_UNSTAMPED` |
| stamp `v` older than the row | nothing, when the block is this edit's own applied block (its request identity is in `txns`) or holds the row's values at `v` and no other line; else held `VIEW_CONFLICT` |
| stamp `v` newer than the row, or `h` not the row's | held `VIEW_INVALID` |
| current stamp, and a line of the block that no value holds, by the import's rule (`log_view.stray_lines`) | held `VIEW_INVALID`, issue `VIEW_BLOCK_TEXT`, because the store keeps only declared values and applying them would drop the other text; this also holds a value edit that adds text. A spacing-only change is formatting |
| current stamp, same values | nothing |
| current stamp, changed values that validate | ONE governed `update` of that row, recorded as a `view_edit`; its request identity is derived from the block's view path `<log>#<key>`, the stamp version and the block's bytes. A field the block does not show, such as an empty child list, is never deleted by the edit |
| current stamp, changed values that fail validation or the leaf | held `VIEW_INVALID` with the diagnostics |
| a stored row with no block | held `VIEW_DELETED`, unless the row was committed after the newest row version that the saved buffer shows, so it was never in that buffer |

- Compare proposed log values with the bound base row rendered and decoded through the same grammar. Apply that field patch over exact canonical values. Parser-discarded spacing is formatting; untouched fields retain their stored bytes. A changed array remains one field, as in the ordinary update API.
- After whole-view validation, edit-back classifies the frame and blocks separately. It applies a valid frame and each valid block through their leaves. Reconcile holds the remaining bytes before re-rendering. A refused or invalid block waits the 10 s invalid window, as an item view does.
- Explicit stale-block resume parses exactly one block and rebases its changes against the recorded row version. It keeps newer fields that the owner did not edit, including fields the log does not display. An unbound template block resumes through ordinary append with key derivation, caller overrides and validation. Existing block holds decode lazily; blocks never enter item-file filename adoption.
- `get_page` reads a log only after authorizing its manifest, every row and complete fields. The same snapshot must render exactly the requested bytes. Parsed frame membership may add restrictions. A stale, edited or partly withheld log answers as absent.
- A log view's holds never stay in place. O2 covers item and manifest views only, because a log hold kept in place would keep every later row out of the view. The held bytes move into the correction and the log re-renders.
- A block hold names its row as `item_key`, so it is released no further than that row. A hold of the whole log view names `view: log`, and `governance.held_targets` releases it only when every row of the collection is released, as the log view itself is (§7).
- Without the `collection-views-v1` release, a log edit waits on disk, as an item edit does. Bytes that an install displaces are held whole as `VIEW_CONFLICT` (§16 A1).
- On quiesce, the release flush (`runtime.RuntimeAdapter.flush`) reconciles pending views before replica publication, under the same view release. Its writer is fresh and runs no census, so under `collection-views-v1` an item view missing from a path its bytes may have reached (`views.may_have_reached_path`) stays pending for the next holder's census; an item view whose bytes never reached its path installs. The drain targets half of the remaining handoff budget (`runtime.DRAIN_SHARE`). At safe checkpoints, it stops starting optional view work after that cutoff or cancellation. An active filesystem operation, preserved-input recovery or required transaction finalization can overrun the share. Displacement and installation finish their custody and durability steps. Completed preservation commits; unfinished views remain pending for the next holder. Replica publication and coordinator release retain the original deadline.

Held **view corrections** appear in `inspect` coverage, in the attention queue (category `collection_view_corrections`), and as read-only views under `Held/`. Inspect and attention count only the corrections released to the caller, so a withheld correction is absent. They are resolved by resuming or by discarding with a reason. A later valid edit of the same view resolves only a correction held at that view path: an invalid edit kept in place, or a deleted view. A correction of displaced bytes, such as a stale-buffer save (`VIEW_CONFLICT`), is resolved only by resuming or discarding it, so a later edit never drops an unreviewed save. Discarding a correction held in place re-renders its view over the discarded bytes.

The watcher is the human's write path, not a bypass. It uses the same leaf functions as MCP, REST and CLI, so the surface-consistency rule holds.

### 7. Governance: row-level, resolved once, enforced in the query layer

**Governance subject per row.** Each row is evaluated as the tuple:
- `path`: its `view_path`, the same path its file has today;
- `ref`: `exomem://<item_type>/<cid>/<key>` (`record`, `plan`, or a declared type's `item_type`, such as `recipe`);
- `type`: the type's `item_type`;
- `tags`: the values of a schema-declared `tags` field, if any;
- `classes`: author-declared values of a schema-declared `classes` field, if any, using the existing class selector's list and normalization semantics; manifest classes classify the manifest and do not silently classify its rows;
- `project`: the project of the manifest;
- `default_deny`: the subject-level default from its type's `default_audience` (§14.6).

The existing pure evaluator, scopes (`paths`, `refs`, `tags`, `types`, `projects`, `classes` and their excludes), standing rules, grants and org caps apply unchanged. **Row-level audience** is therefore authored exactly like any other policy, in `_Governance`, for example with a scope selecting a row's `ref` or `tag`, or a path under the projection root.

No per-row audience column is added. Governance stays in one authored place, and a data write can never widen or narrow its own release (ruled R3).

**Resolved once per operation.** The policy, tombstones and state paths are resolved once per call, which is #1457 fix 1. Every row then evaluates against that resolved policy in memory.

**Store-mode content grants (owner ruling, 2026-10-01).** Content-bound authorization-session grants for structured store results bind to an individual canonical row, not its rendered item file or the shared log view. The basis includes the vault/store identity, stable item reference, row version, canonical content hash and governance subject metadata; current manifest/type contracts and the existing audience, purpose, policy, expiry, revocation and organization-cap checks still apply. It excludes sibling rows, collection generation and rendered-file hashes. The same canonical basis is used when issuing, redeeming and consuming a store grant, before decoding the row's values or body. A manifest has its own authorization basis and is checked separately.

A hidden sibling's edit therefore cannot invalidate an unchanged released row's grant or continuation. A changed row requires authorization against its new basis: a grant for the old content is never carried into a proposed or committed new version. Mutation checks the complete current state and the proposed state inside the transaction; a content grant valid for the former does not authorize changed content in the latter. Whole-view egress still requires authorization for the manifest and every rendered row, rather than widening one row's grant to the log.

File-mode grant issuance, redemption and consumption remain unchanged. Migration does not promote a file grant into a store grant, and missing or stale views never supply store authority. Authored scope selectors and standing-policy semantics remain the existing evaluator's; this ruling introduces neither a new policy language nor a per-row audience setting.

**Uniform-release fast path.** A collection is uniform for an audience when no scope in the resolved policy can distinguish its rows. That means no path selector matches strictly below the projection root, no ref selector names one of its rows, and no tag or class selector can match a row-declared value. Then one decision covers the manifest and every row:
- L6: the whole collection is visible, and filters, sort, limit and aggregates may push down into SQL.
- Below L6: the collection is absent.

**Per-row path.** Otherwise, the query runs in two phases:
1. An identity-only projection (`row_id`, `view_path`, `item_key`, tags) is authorized per row. No value is decoded before this step.
2. The authorized set goes into a temporary table, and filtering, sort, pagination, totals, aggregates, hierarchy and continuation are computed only over it.

The same set feeds `snapshot` (§3). Withheld rows are indistinguishable from absent rows: they do not count, bound caps, create ambiguity or change continuations.

**Caching and leak surfaces.** Release decisions are cached per `(audience, policy fingerprint, collection)` and re-evaluated only for changed rows (§16 A7). The reserved `_Collections/` directory and every surface that can expose a title-bearing view path (listing, backlinks and graph, resolver, indexes, refusal shapes, attention, history links, held views, inventory, sync-conflict copies) authorize the item first (§16 A8), each with a test.

**Composition with existing governance**
- **Mutation** still requires the complete authorized state. If any row of the collection is withheld from the caller, append, update, bulk upsert, revise and rebaseline refuse `COLLECTION_NOT_FOUND`, as they do today. Relaxing this would let a uniqueness conflict or a generation bump reveal a hidden row. It is now a cheap check: the uniform-release decision, or one identity-only pass.
- The precommit governance hook (`precommit_authorize_mutation`) runs inside the store transaction before `COMMIT`. A refusal rolls the transaction back, so no changed row, mutation transition or mutation receipt survives. The separate governance disclosure receipt may record authorization before publication; it claims no mutation commit, including when a later commit fails.
- Idempotent replay rechecks the caller's current authorization before decoding or returning the stored mutation receipt. Request identity grants no access, and a changed policy, audience or authorization-session basis can make a previously valid replay refuse.
- A uniform-row release proof does not authorize the manifest. Manifest visibility is checked separately because a type/ref/tag selector may treat all rows alike while treating their manifest differently.
- The envelope projectors (`project_query_result`, `project_mutation_receipt`, `_LinkProjector`) are unchanged. They run over the store results.
- **Egress of view files** through `get_page`, `find` and other tools resolves the file to its rows. An item view is released exactly as its row. A log view is released only when every row it renders is released to the caller; otherwise the whole file is withheld, so withheld stays absent. Filesystem readers are the owner, who is not subject to scope defaults.
- **Held candidates** keep their rule: they are filtered like items before being disclosed or counted.
- **Cloud cells** use the same code path.

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
| C12 | `bulk_upsert` (#1452 ships first on files, ruled R2) | the store adopts #1452's request and response API unchanged. It is one transaction and ONE transition with N `audit_effects`, and `first_transition` equals `last_transition`. `BULK_UPSERT_AUDIT_DEPTH` and the chain-depth budget are removed. The per-call row cap rises from #1452's file-mode cap to 500 | the cap is raised and a refusal code is retired; no argument changes |
| C13 | size limits | `_MAX_ITEM_FILES` (2,000), `_MAX_COLLECTION_BYTES` (8 MB) and `_MAX_RECORDS` (10,000) stop bounding store collections. The new limit is `COLLECTION_ROW_LIMIT` = 100,000 rows per collection, exposed by `describe`. A log-layout view keeps its 2 MB rendering cap; a log collection past it refuses the write with a remediation to switch to the items layout | limit raised |
| C15 | `schema_memory` | new `subject: "collection-types"` with `inventory`, `inspect`, `validate`, `diff`, `save-collection-type`, `history` and `restore`; `infer` refused (§14.5) | additive; frozen candidates unchanged |
| C16 | `record_memory` | ruled R8: also serves collections of declared types, with the new `action: "transition"` and an optional `collection_type` on `describe`. Nothing else is added (§14.7 schema-byte budget) | additive on the local surface and v5 only |
| C17 | `plan_memory` | a facade over the generic operations (§14.7); wire unchanged | unchanged |
| C18 | manifests | `collection_type:` names the type; `semantic_profile: records\|planning` remain accepted aliases; `link` fields may declare `target.collection_type` and `pin: version` (§14.3) | additive |
| C19 | `activate_context` packet | `generation.collection_types_hash`; the `collections` lane replaces the `records` and `planning` lanes (both kept as aliases); `ANCHOR_KINDS` gains `item` | additive |
| C20 | lease coordinator wire | renew and ordinary release carry `collection_store_head: {store_id, instance_id, commit_seq, head_hash}` as coordination metadata; a separate vault-specific `collections-store-v1` capability fence uses the existing coordinator transaction/token machinery (§16 A3, A5), without changing governance schemas 3/4 | multi-host vaults must explicitly probe this capability before store adoption. Preflight refuses otherwise, naming the exact release to upgrade the coordinator to (ruled N3) |
| C21 | refusal and hold codes | `COLLECTION_STORE_SYNC_PENDING` (retryable), `COLLECTION_STORE_DIVERGED`, `COLLECTION_VIEW_PATH`; held `VIEW_FOREIGN`, `VIEW_MOVED`; the `view_move` effect label (§16) | additive |
| C22 | views | every item and manifest view gains the reserved `exomem_view` stamp, and every log block gains a stamp comment replacing the audit marker (§16 A2) | the stamp is refused in `item` / `changes` and ignored by the payload hash |
| C23 | CLI | `exomem collections migrate`, `adopt-local`, `backup --to/--stdout` | additive |
| C24 | Planning stable-identity content replay | identical normalized `add` with an explicit or natural-key-derived identity and the original insert proven returns `outcome: replayed` in store mode (§4), where legacy file mode refuses | canonical idempotency; narrow compatibility correction, no wire-shape or file-mode change |
| C25 | query rows of a migrated log collection | a row whose file key the adapter inferred from its natural key reports `inferred: false`: the import stores that key as the row's identity, so a later edit keeps it | `record_id` unchanged; the flag now says the key is stored |
| C14 | receipts | `receipt_version: 1` shapes unchanged. `audit_correlation` is the 24-hex `transition_id` of the one transaction | unchanged |

**Frozen hosted candidates** were retired with Hosted in #1691, together with their schema pins. The grouped `record_memory` and `plan_memory` registrations remain the compatibility contract for local and Cloud clients. `minimum_records_reader_version` stays 2 (`deploy/chatgpt/personal-plugin-contract.json`, guarded by `tests/test_connector_guardrails.py`): `audit_reader_version` (1 or 2) is kept per collection and reported as today, and the store has its own `schema_version` that the wire does not expose. The local surface, the command binding and the derived artifacts are regenerated only for C11 and C12.

### 9. Bulk upsert is one transaction

PR #1452's `bulk_upsert` runs in one `BEGIN IMMEDIATE` transaction:
1. check the guard once;
2. plan the rows in input order against the transaction's own view;
3. write inserts and updates;
4. insert one `txns` row with N `audit_effects`;
5. `COMMIT`.

`abort` rolls back on any rejection and reports every row's would-be outcome. `skip` commits the accepted rows. A transaction with only `unchanged` rows writes nothing and does not advance the generation. The spike measures 20 ms for 500 rows (250 inserts, 250 updates) at 10,000 existing rows, and 6 ms for a full-replay batch.

The option-B "bulk audit event" that #1452 deferred is simply the native shape here.

**Sequencing (ruled R2).**
1. **#1457 lands now as the interim.** It brings per-operation authorization, the stat-generation item cache, batched guard rechecks, and the manifest parse cache. File-mode collections keep those until the legacy window closes (R7). The store reuses #1457's once-per-operation policy resolution.
2. **#1452 ships now on files**, with its per-call row cap enforced so a single bulk call holds the writer lease for no more than about 5 s. Its per-item chained events and `BULK_UPSERT_AUDIT_DEPTH` apply to file mode only.
3. **The store implementation adopts #1452's API unchanged.** That covers the action name, `rows`, `on_reject`, `source`, the guard, per-row outcomes and codes, and the batch receipt. These storage-mode differences apply:
   - it drops `BULK_UPSERT_AUDIT_DEPTH` and the chain-depth budget, because a batch is one transition;
   - it raises the per-call cap to 500 (`BULK_UPSERT_MAX_ROWS`, reported by `describe`), since the store takes 20 ms at 500 rows;
   - `first_transition` equals `last_transition`.
   - file-only directory census bounds, including the 2,000-file `COLLECTION_ITEM_LIMIT`, do not limit canonical store rows. Store `describe` omits that filesystem refusal; the 10,000-row store acceptance fixture remains valid.

   A collection's cap follows its storage mode, so a client written against the file cap keeps working after migration.
4. When #1452's change is archived into the canonical `records` spec before this one, this change gains a `records` MODIFIED delta for #1452's bulk requirements carrying exactly those differences (task 0.4). Until then, #1452's requirements are not canonical and cannot be modified here.

### 10. Migration: verifiable, reversible, zero-downtime

**Switch.** Complete admitted canonical collections, current manifests and transition intent determine ownership (§16 A5). The version-two `mode.json` remains a compatibility hint and cutover preimage. Migration updates its entries and authority epoch, but a missing entry never returns an admitted collection to files. Explicit restore binds a verified snapshot to trusted enrollment and the current fence. Proved file-only collections retain their existing authority; datasets remain file-canonical. General migration runs from the GA release (P3.6), which adds `collections-migrate-v1` to the release set.

**Preflight (`maintain_memory(mode="collections-store", dry_run=true)`)** reports, per collection:
- the rows found;
- the legacy audit status;
- the round-trip result;
- blockers.

Blockers are duplicate or ambiguous identities, schema violations, unsupported versions, and datasets (which are skipped, since they stay files). A vault with blockers does not migrate. The owner fixes them in files first, as today. This step is read-only and changes no file.

**Import and verification** (the migrator, pure and deterministic). For each collection:
1. Read the files with today's adapters (`MarkdownItemsAdapter` / `MarkdownLogAdapter`), with full authority.
2. Write the rows with the same `item_key`, `natural_key`, `values_json`, `body`, `payload_hash` and `view_path`. Write the manifest text verbatim as `manifest_version` 1, and the held candidates with their existing ids.
3. Parse the legacy events from `log.md` and every archive with the uncapped streaming reader filtered per collection (§16 A6), not the bounded `_audit_events` (which returns empty past 8 MB, 10k events or 128 archives) or the 2048-deep chain walk. Import every reachable event as a `txns` row with `operation: legacy_import`, `legacy_event_json` verbatim, and `prev_event_hash` chaining. Then append one `legacy_import` checkpoint transaction. `legacy_audit_status` is set to the legacy inspector's result, so a `gap` stays a `gap` and is never blessed.
4. **Round-trip proof.** It must pass for the vault to migrate:
   - (a) row count equals legacy item count;
   - (b) for every row, `parse(render(row))` equals the row's values, body, key and natural key;
   - (c) `payload_hash` recomputed equals the legacy `_payload_hash`;
   - (d) the imported chain head equals the manifest `record_audit` / `plan_audit` head, and the event count equals the legacy chain length;
   - (e) the manifest text is byte-equal;
   - (f) the legacy inspect status equals `legacy_audit_status`.
   Render drift, where `render(row)` differs in bytes from the current file, is recorded, is not a failure, and changes nothing. Files are not rewritten at migration.
5. Record `projection_state` with `published_sha256` equal to the hash of **the exact bytes the importer parsed**, captured in the same read (§16 A5). The existing files are therefore already "current views", unstamped until their first re-render. A file changed between parse and publication is a human edit by construction.

**Zero downtime** uses `managed-service-upgrades`:
1. The target's candidate probe declares the offline migration `collections-migrate-v1`, its release fence (`capability.offline_migrations`). A target that keeps the fence off declares none, and its upgrade runs exactly as before. A probe whose declaration raised is recorded as `target migration probe failed`, never as declaring none. The supervisor keeps the declaration as private verified metadata of that candidate, beside its optional state support; the `{python, version, state_descriptors}` target is unchanged.
2. The standby worker, which holds no lease and no ownership, pre-imports into a staging store from the live vault read-only, as the last step of its warm (the `collections_migration` cutover component, reported only while the fence is on). It records each collection's basis in that store: the manifest hash, the container snapshot and source hashes, the held hashes and census, the audit head, and the proof's reachable count, reachable head, ordered event digest and bounded inspection status. The vault-wide audit segment census is excluded, so an unrelated activity-log write does not invalidate a basis. On a vault whose S1 store is live, the staging store is a backup-API copy of that store plus the imports (copy-then-swap). The pre-import stops 10 s before the warm budget ends, and the component is ready once it ran, however it ended, so a failed or slow pre-import never holds the cutover: it leaves no staging, and the handoff runs no collections step. The standby pre-imports only when the supervisor's non-secret `EXOMEM_STANDBY_COLLECTIONS_STEP=1` says that this handoff can run the step: the target declares it and the operator file is usable. Otherwise it removes the staging an earlier warm left. A vault with nothing left to import gets no copy of its live store.
3. The active worker keeps serving reads and writes.
4. At handoff, after the old worker has provably exited, the supervisor runs the migrator only while the target declares the migration, the vault holds the standby's staging or a published store that still carries its migration intent, and the protected operator file is usable (D1, below). The migrator runs one fresh audit scan, captures each collection and compares its basis. When none changed, the pre-import and its proofs are carried forward. When one changed, a fresh candidate assembles the unchanged proved imports and imports and proves only the changed or added collections (§16 A11). A copied S1 store is carried forward only while the live store's head and bytes are unchanged since the copy. A moved head abandons the handoff (D4). Changed bytes under the same head, such as a view the serving worker checked, re-copy the store. The supervisor gives the migrator 15 s (`service_manager.MIGRATION_SECONDS`), and passes it an absolute monotonic deadline 6 s before that wait ends (`MIGRATION_FINISH_SECONDS`). The child's interpreter start counts against its own budget. The reserve covers the steps from the seal on, the child's exit and the supervisor's exit proof: 3.0 to 3.3 s measured on a test vault with about 3,900 items, doubled because replica publication grows with the store. Every comparison, copy and scan the migrator runs takes that deadline. If the delta is not sealed by then, the migration is abandoned (§16 A11). Then, in this order, the migrator publishes the store atomically into the state root, CAS-cuts the separate collection-store capability fence, reacquires the lease, records the `collections-store-v1` compatibility descriptor (§16 A5), publishes the replica and installs the marker. The standby is promoted after that. The step writes the store, its marker and replica, the state manifest's descriptor and the coordinator's fence, and no source that a graph snapshot proves, so a handoff whose only migration is this step keeps promotion's checkpoint comparison; only a state migration re-runs the whole source proof (5.2 s against 0.005 s on an invented 8,000-page vault).
5. Writes pause only for the ordinary bounded handoff. A failed proof leaves no store, no marker and an unenrolled fence, the vault untouched and the old release restorable. The release completes in file mode, and the migrator's report in the service journal names the failing collection and check.
6. **Abandon.** An abandoned migration publishes nothing and exits 0, so the release completes in file mode. The migration retries at the next handoff. The migrator classifies how its step ended by the durable state it left, never by where it raised. A refusal or a fault before publication is abandoned, because nothing changed and failing the upgrade would only stop the service. A fault after the marker was admitted, such as a lease handback that raised, is `migrated`. A published store that still carries its intent is `pending`. The supervisor then reruns the migrator once, after it waited out the failed migrator's lease TTL (`EXOMEM_WRITER_LEASE_TTL`, 30 s by default), outside the cutover budget and before the replacement starts. One rerun and no more: each attempt costs a lease TTL of downtime, and a fault that persists does not clear on a second try. A store still pending after the rerun leaves the replacement to start, and the next handoff, its resume or the owner's offline apply finishes it, each comparing every collection with its import basis first and superseding the store when a file changed. On a first enrolment no write in that state can fork the published store, in either of two ways. When the fault came after the fence cut, the cut fence refuses every writer inside the lease, so the vault serves reads only until the step finishes. When it came before the cut, the fence is unenrolled, so the vault stays in file mode, and its writes commit to the files, which are still canonical. In both cases the next step's comparison finds every file that changed and supersedes the store with an import of it, so no write is lost. The migrator that raised keeps its unadmitted lease until the lease expires, so a step run inside that window is refused and stays `pending`, which is why the rerun waits first. The record names the rerun's outcome and keeps the first attempt's as `first_attempt`. Only `unsafe` fails the handoff: the live store is set aside beside the marker that routes it, as when a swap stopped between its two renames, or the store or marker it left cannot be read. A migrator that the supervisor's wait stopped is classified the same way, by what it left: a cutover that published its store finishes once begun and can outlast the finishing reserve on a large store, so failing the upgrade then would make every resume repeat it. The record carries `code: timed_out` beside that outcome, and the stopped run gets the same rerun. Only in a handoff that also migrated state, which the stop may have cut, does a timeout fail the upgrade as `timed_out`. The migrator's output reaches the service journal once its exit is proven. The handoff record carries the step as `migration.collections`: a skip with its reason, or the migrator's outcome and refusal code, clamped to the record's shape. Non-managed installs migrate with `exomem collections migrate` (§16 A11): `--dry-run` runs the preflight, `--prepare` pre-imports beside a running service, and `--apply` runs the cutover with the service stopped and the operator credential. Offline, apply refreshes staging with eligible retained imports instead of abandoning when a collection basis or the live store changes.

**Operator credential at a handoff (D1, ruled by the operator).** The first fence cut needs the coordinator's operator credential, which the service and its standby never hold. The supervisor reads `operator.env` from the directory of the unit's first service environment file (`service_manager.OPERATOR_ENV_FILE`), beside `service.env`. It takes only `EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN` from it, at the moment it spawns the migrator, and gives it to that child's environment alone, never on a command line. The file is never an `EnvironmentFile=`, so neither systemd nor a worker loads it. It must be a regular file of the service user that no other user can read or write, opened without following a symlink. This prevents a credential that another local user could read, replace or redirect from cutting the store fence. When the check fires wrongly, the managed path does not migrate, the release completes in file mode with the reason in the handoff record, and the owner fixes the file or migrates offline. A missing or unreadable file never fails the upgrade. The supervisor's release decides all of this, and a seamless upgrade replaces only the worker, so the launcher must run a release with this step.

**Reversal**

GA application rollback retains SQLite authority. Before activation, qualify an earlier application artifact that supports the admitted schema and preserves original-file archives. Prove its admission, reads, writes, restart and archive preservation against a migrated disposable vault. Pin the rollback artifact and its maintained installation route in release evidence. Restoring that application does not retire the store fence or return collections to files.

Reverse export is whole-vault. A store-native collection or a file-mode bound violation blocks the entire export and leaves authority unchanged. Partial export remains outside this contract. This restriction concerns downgrades; forward migration and snapshot backups retain their existing paths.

- **Before the first store write:** set the mode back to `files`. The files and their legacy chains were never touched.
- **After store writes:** `maintain_memory(mode="collections-store-export", dry_run=...)` renders every row back into the legacy canonical layout:
  - item and log files with audit markers;
  - manifests with `record_audit` / `plan_audit` heads;
  - per collection, one legacy-valid v2 `rebaseline`-shaped checkpoint event naming the store transitions it summarizes, content-free.

  Legacy inspect then reads `acknowledged_gap` with the discontinuity documented, never a silent `ok`. The export is atomic per collection through `batch_atomic_write` and preview-first. After it runs, the marker is retired by renaming it to `mode.json.exported-<utc>`, and the replica and the live store are tombstoned by renaming each to `collections.sqlite.exported-<utc>`, so no host can adopt them (§16 A5). A marker mode `exported` is never written: released launchers accept only `mode: "store"` and would crash on it. Downgrading to a pre-store release requires this export first, because `state_migration` refuses the unknown descriptor.
- **Test:** files → store → export → files is byte-equal for collections not written in store mode, and legacy-valid for collections that were.

### 11. Backup, restore and hosted cells

- **Consistent snapshot primitive.** `sqlite3.Connection.backup` into a staging file, `journal_mode=DELETE`, `PRAGMA integrity_check`, fsync, atomic rename. The vault replica (§1) uses it, and so do `exomem collections backup --to <file>` (and `--stdout` for `restic backup --stdin`) and portability export. The spike measures 77 ms at 10,450 rows (15.8 MB with history).
- **restic and vault copies** back up the vault. The replica is a single consistent file, and no part of the live store is ever in the vault. The live store is **excluded entirely** from file-level backups: copying its main file without or with its WAL is not consistent. The supported deployment has no restic timer; an external backup that needs the store reads `exomem collections backup --stdout` (§16 A9).
- **WAL checkpoint.** A passive checkpoint runs after the projector drains, and a truncating checkpoint runs on quiesce, so the WAL stays bounded.
- **Restore.**
  - Check the snapshot's `integrity_check`, `schema_version`, and that its `store_id` matches or restore is explicit.
  - Re-render every view into a staging tree and compare it with the vault.
  - Publish the store, then drain the projector.
  - A restored store older than the vault's views surfaces the difference as held `VIEW_CONFLICT` corrections, never as silent overwrites.
- **Cloud cells.** The live store sits in the cell state root on the tenant volume (`external-canonical`), one per cell, so tenant isolation is unchanged. Quiesce flushes the replica. Portability export includes the quiesced snapshot at its vault path as canonical data. It is a single-file `journal_mode=DELETE` backup-API copy, and the live store's files are never exported. Restore stages and validates as above before publication. `hosted-vault-portability` is modified accordingly.

**Operator export from a selected backup.** Restic already captures the vault and host roots from a stopped claim or snapshot clone. Restore both roots into exclusive disposable scratch. Prepare its replica in place; a second full vault copy adds no custody. Derive one source path from reviewed original placement and validate its state manifest against the original vault identity. When that placement cannot be established, require one reviewed source directory instead of scanning or trying fallbacks. This adapter changes neither the live cell nor the selected backup.

Copy the stopped source database and its WAL into private working storage before SQLite opens them. Run the shared backup primitive against that clone, retaining the source schema in the standalone output. Validate integrity, history, marker bindings and pure adoptability conditions before replacing the scratch replica. Use another private migrated copy for current validation of supported older schemas. Compare the previous replica by chain ancestry and compatible lineage; a higher sequence alone selects nothing. Reject unfinished export, incompatible pending creation and divergence without attempting recovery.

An old replica used only as an ancestry witness may lack later collections and carry a valid historical pending-create target. It still passes structural, identity, overlapping ownership, divergence and departure checks. The caller proves head ancestry and compatible lineage before replacement. Every canonical replacement and restored replica retains the full current-marker checks.

Keep the existing tenant-encrypted tar format and its single `vault/` wrapper. Select members and prune disposable directories through the existing portability classifier. Host state, SQLite companions and interrupted publication work never enter the artifact. Install the validated replica with held identity checks and a directory flush. A retry validates an already-prepared replica through the same path. The archive process then reads scratch without write access.

**Staged Cloud restoration.** Extract shared replica validation from ZIP restore and use it before Cloud restore renames any destination. Cloud restoration additionally imports and proves file-routed collections in private staging through existing preflight helpers. Store-routed collections retain canonical rows from their validated replica; rendered views never replace those rows. Recognize the existing single `vault/` tar wrapper and reject mixed roots. Generic ZIP restoration retains its existing handling of invalid file manifests.

Restored bytes grant no destination writer authority. Destination coordination remains P1b.7a, including matching fences, head reconciliation and fresh tenure. A generation-2 marker still refuses a fresh coordinator, as the deployment contract requires. Export does not restamp an instance, reset a fence or enroll a coordinator. Restore receipts describe restored bytes; collection admission and readiness need separate proof before enablement.

#### Portable collection export follow-up

User-facing vault exports use the existing ZIP portability owner. ZIP64 supports large archives; standard DEFLATE compresses text without requiring a platform-specific extractor. Already compressed content may remain stored. The separate encrypted operator recovery procedure does not determine the user-facing archive format.

A complete export adds each collection's descriptor, declared schema and current rows from the same consistent snapshot. JSON preserves typed values, authored row bodies, nulls, nested values, stable identities, row versions and provenance. CSV provides the complete tabular view alongside JSON. The consistent SQLite snapshot retains the canonical audit and restore state. Readable files neither become a second authority nor replace legacy reverse export.

Reuse the existing streaming ZIP writer, manifest verification and safe portable paths. Stream rows in bounded batches without building the complete collection in memory or generating per-row Markdown. The export must cover rows beyond ordinary query pagination and return no complete result after partial generation.

This follow-up does not gate the confirmed GA scope. Existing query CSV/JSON output and current collection modes remain the GA interface. A human table editor and selective item publication remain deferred.

### 12. Performance

**Owner ruling, 2026-10-04 — S1 only.** The first NEW owner-only Records summary collection may pass its independent S1 release gate before full public inspect p95 <15 ms and guarded append p95 <20 ms are met. Continue measuring and reporting both as optimisation targets, with scope, actual paths, data size and environment; do not mark the parent performance gate passed or claim those latencies. The general phase/GA targets below remain unchanged. All S1 correctness, permissions, integrity, crash/retry, compatibility, publication/recovery, portability and other applicable bounds remain prerequisites. This allows the necessary parent foundation to be delivered dark before its full P1a phase gate; it does not authorize existing-collection migration, arbitrary-type rollout or renewed CPU tuning instead of the product outcomes.

**Owner ruling, 2026-10-19: latency does not block GA.** Measurement showed that the 15 ms inspect target is out of reach at 10,000 rows with the current inspect response. The owner then ruled that latency targets must not block shipping, because these latencies are small next to the benefit over the file-mode system. For scale, the file-mode guarded append after #1457's fixes measured 625/706 ms p50/p95 at 1,000 items (`docs/verification/records-append-latency.md`). The store service path measured 48.19/58.81 ms in one A/B arm of a different harness (below). The append, inspect, bulk, acknowledgement and per-stage targets in P1a.15, P2.8 and the structured-collections latency requirement stay optimisation targets. Query latency no worse than the file path stays a required bound, because it keeps that comparison true for queries. Each phase gate measures and reports them on Linux and native Windows/NTFS, with scope, path, data size and environment. A missed target is reported as missed, never as passed. Correctness, permissions, integrity, crash and retry, recovery, compatibility, portability, resource-budget and parity gates are unchanged, and so are the other P3.6 prerequisites.

**Targets**, in the release acceptance harness, as extensions of `scripts/measure-records-append-latency.py` from #1457:

| Operation | Target | Today on `main` (#1457) | Spike (storage engine only) |
| --- | --- | --- | --- |
| guarded append, p95, N=10,000 | < 20 ms end to end | 5,078 ms at N=1,000 (append only); 12.2 s round trip | 0.72 ms p95 (0.83 ms at N=1,000) |
| bulk upsert 500 rows | < 1 s end to end | ≈ 500 serial appends | 20 ms; replay 6 ms |
| query, filter + sort + limit 50, N=10,000 | same results as the file path; p95 ≤ 50 ms per-row governed, ≤ 5 ms uniform | 7.6 s refresh read at N=1,000 | 44 ms per-row path, 0.4 ms uniform fast path |
| snapshot, N=10,000 | < 250 ms, off the ack path | n/a | 77 ms |
| item render and exact pending state | on the ack path, < 4 ms p95; physical publication measured separately | inside the write | 0.6 ms p95 (spike); 2.6–3.8 ms p50/p95 for the whole sync-projection storage layer (#1457 prototype) |
| guard refresh through full public `inspect` | p95 < 15 ms | 416 / 546 ms at N=1,000 after #1457's fixes | 0.02–0.04 ms guard extraction only (#1457 prototype) |

**Diagnostic baseline, 2026-10-17.** Source `b6bffc9bf`, script SHA-256 `2689cb9c883ca38ca6ebd7b0a1dfbc6768b3b7870582b6785944be12ecea8114`, Python 3.14.6 and SQLite 3.53.1. These sequential Linux/WSL runs used ext4 and the isolated store-preview dispatcher path. Each run measured 30 samples per append, inspection and refusal case, plus one bulk call. The writer boundary was local; coordinator requests were disabled.

| Seeded rows | Guarded append p95, ms | Full inspect p95, ms | Complete-audience append p95, ms | Partial-audience refusal p95, ms | Bulk 500 elapsed, ms |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 53.57 | 38.32 | 46.47 | 15.29 | 8,289.31 |
| 10,000 | 48.54 | 704.45 | 48.45 | 23.31 | 4,749.60 |

The public inspections proved seeded/full/partial counts of 1,000/1,000/900 and 10,000/10,000/9,000. Each refusal preserved the canonical state and rendered-view hashes. Each bulk call stored exactly 500 new rows without pending publication. Peak RSS was 207,060 KiB and 406,144 KiB respectively, measured by `time -v` over each parent and child run.

Report SHA-256 values: `store-latency-1k.json` is `aebc2634b3771312d002983dde372b24af5c98211f7af25e4261aa8f84242124`; `store-latency-10k.json` is `e309e2284f9b89e9f74dcb8065255d4cb9059ef6bf9c5ebd8a9f4a984e6b6c34`. These runs miss the phase targets. They do not establish a latency curve or a speed change. Native Windows/NTFS, the managed coordinated-store path, commit/fsync tracing and query/resource acceptance remain unmeasured here. P1a.15 stays open.

**Audience-churn repair, 2026-10-17.** A cache-only reproduction used the diagnostic's complete, partial and returning complete audiences. The fresh 10,000-row state fitted the 16 MiB budget. Removing and reinserting 1,000 released rows grew retained dictionary tables enough to install the overflow sentinel. Compacting those tables before fallback restores incremental reuse without increasing the budget. The returning audience loads 1,000 contributions; its next inspection loads none. True overflow retains the exact uncached path.

The same 30-sample workload at source `6f29fae7c` measured full-inspection p50/p95 of 166.96/187.90 ms, versus 649.12/704.45 ms before the repair. Guarded append measured 36.92/40.38 ms; the single bulk call took 4,579.06 ms and stored exactly 500 rows. The complete/partial counts and unchanged refused state passed again. Peak RSS was 416,324 KiB. The script changed only to retain existing bulk timing evidence: SHA-256 `1a4fcb9700a78687ab7dd6b214e12c648910b5eabfc9f44aa15b4ded92405af2`. The report `store-latency-10k-fixed.json` has SHA-256 `62969aa1ea705b577306a9c3c972e68fb74b0b5df061afd3e84e3b09ca4f714d`.

Pairing the same 30 operation positions gives a median inspection difference of -479.56 ms. A paired bootstrap gives a descriptive 95% interval of -492.27 to -365.47 ms. Each arm has one sequential run, so this interval does not control temporal host effects or estimate variation between machines or runs. These results still miss the phase targets and establish no general scaling curve.

**Pending-history inspection, 2026-10-18.** Source `00760a87a` reuses normalized configured selectors and counts admitted, never-published history pages without rendering their bytes. Published pages retain exact digest comparison. Inspection retains page authorization, existence and canonical integrity checks.

The unchanged diagnostic script has SHA-256 `fa5dc69d089217faf017997ece60424f129246b839e8077c1788fc44f0ba4088`. Sequential Linux/WSL runs used Python 3.13.14, SQLite 3.53.1 and isolated store-preview dispatch. Each run measured 30 operation positions, a 10% ref-withheld audience and one bulk call.

| Seeded rows | Guarded append p95, ms | Full inspect p95, ms | Bulk 500 elapsed, ms |
| ---: | ---: | ---: | ---: |
| 1,000, before both changes | 25.49 | 395.59 | 512.52 |
| 1,000, after both changes | 20.97 | 41.32 | 444.35 |
| 10,000, after both changes | 22.69 | 180.72 | 507.81 |

Both sizes prove complete/partial row counts, unchanged state after refusal and exactly 500 stored bulk rows. At 1,000 rows, selector normalization alone measured inspection p95 335.35 ms. The subsequent shortcut removed the remaining unnecessary history render.

The paired inspection median difference is -332.95 ms, with a descriptive bootstrap 95% interval of -334.50 to -330.95 ms. The append median difference is -4.08 ms, with an interval of -4.42 to -3.83 ms. Each arm has one sequential run. These intervals do not control host timing or estimate variation between runs; they establish no installed improvement or scaling curve.

Report hashes: before-1k `4f49a8e32fac29b27dc7d954c4cab1276a60980e381e5a9305279d88e41a0736`; after-1k `5c8f7918e776445b141ae80c6e244aba84dcf50efc1d2409b492d5e4bd24b414`; after-10k `96c2473a0cd5dde3cce063e4625442b4bde19668fa557cee3416dfe9c4d73fae`.

Append and inspection still miss the phase targets. Native Windows/NTFS, managed coordination, commit/fsync tracing, resources and publication overlap remain outside these diagnostics. P1a.15 stays open.

**Serving publication reuse, 2026-10-18.** The next comparison uses normal item commands through the service dispatcher and a real loopback coordinator. Both arms use single-host HTTP, Python 3.13.14 and SQLite 3.53.1. The before source is `81ec35728`; the after source is `e56ffea5e`.

The frozen probe hashes are `b55b22726885b4650c64ba635debd398adadb6663f81500987252247b3340883` at 1,000 rows and `298a488859b241b6c84af8f82502d1dc3d22615f7baef8c2275b85fc35dfee7f` at 10,000 rows. Each size uses the same probe in both arms. The larger probe adjusts only the existing source-version count limit.

Each arm retains all 30 successive inspect/append positions, including the first position and every tail. P95 uses linear interpolation. The interval resamples paired differences at those positions 50,000 times. It is descriptive: the arms ran sequentially about one hour apart, without concurrent host control or independent run replication.

| Initial rows | Operation | Service p95 before → after, ms | Median difference, after minus before, with descriptive 95% interval, ms |
| ---: | --- | ---: | ---: |
| 1,000 | Inspect | 40.30 → 40.16 | -0.18 [-1.03, 0.77] |
| 1,000 | Append | 70.09 → 72.25 | +1.64 [0.20, 2.82] |
| 10,000 | Inspect | 1,320.95 → 190.13 | +11.80 [9.45, 14.44] |
| 10,000 | Append | 961.96 → 134.44 | +1.93 [0.16, 4.17] |

The 1,000-row inspection interval includes zero: no change. The other median intervals are positive; typical requests are slower in these observations, while the large-collection tails shrink. The comparison establishes no causal or installed gain.

Initial complete and partial inspections precede these 30 positions. After the correction, they take 88.71/49.23 ms at 1,000 rows and 414.72/286.95 ms at 10,000 rows. Bulk calls commit all 500 rows in 454.98 ms and 487.16 ms. Exact seeded values, complete/restricted counts and unchanged refusal fingerprints pass in all four arms.

The fixture keeps item-view publication dark and disables due/capture carriers, embeddings, media and watchers. Worker timing excludes checkout entry and exit; inclusive call spans overlap. Native Windows performance, installed acceptance, resources and the strict phase targets remain open.

The retained `measurement.json` hashes bind the observed values:

| Arm | Rows | SHA-256 |
| --- | ---: | --- |
| Before | 1,000 | `816c0c08985415e1055f2692b5d065c766b9a52f95c5e409cb8bcd3a7f6c27f2` |
| After | 1,000 | `52ea87ac32a626263ffbaa5aafe042d0625dd2d45584621325efb4b8cc5207d6` |
| Before | 10,000 | `c6a859d93944c36139da2f8dcddc485b6fb30c0b2386cdfad2ad0d6e569a2777` |
| After | 10,000 | `a5e903736438dd720c58973a3a4d2aa855570199a7d9e96b2452bc87558ad35f` |

**Baseline at `f8cd6c119`, 2026-10-19.** This rerun uses the same frozen service probes, Python 3.13.14 and SQLite 3.53.1, on a Linux/WSL host with no free swap. Each size ran once.

| Initial rows | Inspect p50/p95, ms | Append p50/p95, ms | Partial refusal, ms | Bulk 500, ms |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 46.03 / 78.49 | 95.81 / 226.53 | 101.35 | 632.65 |
| 10,000 | 101.44 / 182.25 | 74.29 / 179.85 | 341.18 | 497.06 |

The 1,000-row tail is most likely host noise: one instrumented run of the same probe minutes later measured append p50/p95 66.55/75.50 ms. Exact values, counts and unchanged refusal fingerprints pass at both sizes.

The isolated diagnostic (`scripts/measure-records-append-latency.py`, 10,000 rows, store-preview, phase probes) measured guarded append p50/p95 28.81/36.53 ms and inspect 107.98/134.14 ms; the inspect is almost all thread CPU. Four exclusive stages exceed their p95 budgets: dispatcher 7.69 ms (budget 5), lease 7.36 ms (budget 2), governance 10.63 ms (budget 2) and store transaction plus view 10.38 ms (budget 4). The 10,000-row inspect response is about 330 KB; its 2,000 `source_versions` entries are 324 KB of that, and terminal egress scrubs about 4,100 strings per call.

**Coordinator request attribution, 2026-10-19.** Two instrumented 1,000-row runs used the same probe with a real loopback coordinator. The first timed each coordinator request on the client and on the server; the second recorded one call stack per request. In the first run, 27 of the 30 warm appends made eight coordinator requests; the other three also carried a background renew. The probe's fixture releases only `collections-migrate-v1`, so its summary-route check also proves ownership. With the shipped release set, a dispatch makes one proof fewer.

| Requests per append | Call sites | Client p50, ms | Server p50, ms |
| ---: | --- | ---: | ---: |
| 4 status reads | canonical ownership proof: one on the caller thread, three inside the store dispatch | 1.2–1.4 each | 0.5 |
| 2 status reads | store runtime fencing admission: checkout, and the write-authority check before the transaction | 1.3 each | 0.5 |
| 2 acquires | `ensure_writer` in the operation guard and in the mutation guard | 11.3 each | 10.5 |

Each acquire's server time is mostly its `COMMIT` (6.6 ms, WAL with `synchronous=FULL`) and closing its per-request connection (3.4 ms). The close is consistent with a last-connection WAL checkpoint; no system-call trace was taken. The coordinator windows total 31.38/47.13 ms p50/p95 of a 66.55/75.50 ms append. Each inspect also makes four or five status reads, about 6 ms. Removing every coordinator request would still leave the append above 20 ms.

**Coordinator connection reuse, 2026-10-19.** The coordinator's lease store now keeps one SQLite connection, and a store dispatch proves ownership once for its own checks. Four 1,000-row runs alternated before (`f8cd6c119`) and after. Two of them lost their per-append totals to a join defect in the probe's report, not in the product. Their per-request timings are complete:

| Arm | Acquire client p50, ms | Acquire server p50, ms | Acquire `COMMIT` / close p50, ms | Status reads per run |
| --- | ---: | ---: | ---: | ---: |
| Before (2 runs) | 11.86, 11.60 | 11.02, 10.88 | 6.7 / 3.5 | 353 |
| After (2 runs) | 4.95, 4.63 | 3.95, 3.88 | 3.5 / 0 | 285 |

The intact before/after pair shows the append p50/p95 moving from 65.48/75.71 ms to 48.19/58.81 ms. The coordinator windows per append fall from 30.94 to 14.50 ms p50, and the requests per append fall from 8 to 6 (from 7 to 6 with the shipped release set). The paired median append difference is −17.27 ms, with a descriptive bootstrap 95% interval of −18.53 to −14.91 ms over 30 positions. The inspect difference is +0.22 ms [−1.81, 2.15]: no change. One sequential pair on one host establishes no installed gain. Append still misses 20 ms.

**Isolated diagnostic, Linux, 2026-10-19.** `scripts/measure-records-append-latency.py` at `99f136bd8` (script SHA-256 prefix `cb92b7af2bbd`) ran on Linux/WSL ext4 with Python 3.13.14 and SQLite 3.53.1. Its writer boundary is local and its coordinator is off, so these rows do not compare with the service-path rows above. Store runs take 30 samples per operation. File runs take 10 and lift the 2,000-item file caps; the shipped file path refuses 10,000 items. Each size ran once. Peak RSS comes from `/usr/bin/time -v`.

| Mode | Rows | Guarded append p50/p95, ms | Inspect p50/p95, ms | Bulk 500, ms | Peak RSS, MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| Store | 1,000 | 19.39 / 24.25 | 30.89 / 35.46 | 486.6 | 183 |
| Store | 10,000 | 21.88 / 24.15 | 86.94 / 102.88 | 500.9 | 373 |
| Files | 1,000 | 493.08 / 550.38 | 413.28 / 439.07 | not measured | 173 |
| Files | 10,000 | 3,615.70 / 3,726.18 | 5,423.31 / 5,553.12 | not measured | 358 |

Store append and inspect miss their 20 ms and 15 ms p95 targets at both sizes. Bulk 500 meets 1 s.

The phase probes withhold 10% of the rows by ref from a partial audience. The complete audience appends to that collection. The partial audience's append is refused with `COLLECTION_NOT_FOUND`, and every run checks that the refusals leave the canonical rows and rendered views unchanged:

| Platform | Rows | Complete-audience append p50/p95, ms | Partial-audience refusal p50/p95, ms |
| --- | ---: | ---: | ---: |
| Linux | 1,000 | 19.52 / 22.45 | 11.23 / 11.93 |
| Linux | 10,000 | 20.49 / 24.69 | 11.33 / 12.28 |
| NTFS | 1,000 | 115.12 / 124.68 | 85.45 / 143.78 |
| NTFS | 10,000 | 117.09 / 286.78 | 83.99 / 90.48 |

The NTFS complete-audience p95 at 10,000 rows, 286.78 ms, is the worst append in this gate; its p50 stays at 117.09 ms.

**Query bound.** The harness runs four Records queries with the same arguments in both modes: an `eq` filter, a `contains` filter, a 30-day date window sorted with limit 50, and `count`. At `36e95b57f`, before the fix below, every case refused in both modes at both sizes with `RECORD_RESPONSE_TOO_LARGE`. Each Records query response listed the source version of every item. Above about 500 items that list alone passed the 64 KiB response cap: a one-row query answered at 400 rows and refused at 600. Every Records query now lists the manifest and the sources of its returned rows, as a Planning query already did. That covers the internal callers too. The independent review reproduced the failures in scaled runs with a lowered cap: plan progress reported `query_unavailable`, and the current-state query refused, which its caller turns into no answer. At the shipped cap with 600 store items, the current-state query also refused before the fix. After the fix, every case answers in both modes with the same total, row count and ordered subjects:

| Rows | Case | Store p50, ms | Files p50, ms | Files ÷ store |
| ---: | --- | ---: | ---: | ---: |
| 1,000 | `eq` filter | 38.04 | 272.13 | 7.2 |
| 1,000 | `contains` filter | 36.67 | 273.73 | 7.5 |
| 1,000 | date window, sorted, limit 50 | 40.16 | 284.54 | 7.1 |
| 1,000 | `count` | 37.98 | 267.15 | 7.0 |
| 10,000 | `eq` filter | 414.48 | 2,857.61 | 6.9 |
| 10,000 | `contains` filter | 413.18 | 2,790.05 | 6.8 |
| 10,000 | date window, sorted, limit 50 | 427.57 | 2,931.10 | 6.9 |
| 10,000 | `count` | 429.54 | 2,858.66 | 6.7 |

The required bound holds on Linux: store query latency is below the file path's at both sizes. Medians are compared because the arms take different sample counts. The 50 ms per-row governed query target at 10,000 rows is missed. That cost does not come from the source list: the same cases took 439–465 ms to refuse before the fix.

**Acknowledgement commits.** Each store run traces one guarded append. The invoking thread runs one `BEGIN IMMEDIATE`…`COMMIT` and one read `BEGIN`…`ROLLBACK` on the store connection, and no other thread uses it. The pending view hash is written in that one commit. The invoking thread also makes one Python fsync, which took 3.06–3.12 ms: as the mutation boundary exits, `writer_lease._bump_commit_generation` replaces the vault's commit-generation file through `durable_write.replace_text`. That write predates the store and runs in file mode too. A later change replaces the counter without that fsync. SQLite's own WAL sync is not a Python call and is not counted.

**Isolated diagnostic, native Windows/NTFS, 2026-10-19.** The manual `store-latency-windows.yml` lane ran the same script at `99f136bd8` on a GitHub `windows-latest` runner, with Python 3.13.16 and SQLite 3.50.4 (run 39327763962). The sample counts match the Linux runs. File mode at 10,000 rows did not finish within the step's 40-minute limit, so it is not measured.

| Mode | Rows | Guarded append p50/p95, ms | Inspect p50/p95, ms | Bulk 500, ms |
| --- | ---: | ---: | ---: | ---: |
| Store | 1,000 | 113.10 / 132.03 | 200.91 / 212.64 | 1,425.8 |
| Store | 10,000 | 114.97 / 120.66 | 329.99 / 342.28 | 2,032.8 |
| Files | 1,000 | 16,816.57 / 25,914.40 | 7,161.13 / 7,554.82 | not measured |

Every NTFS target is missed, and bulk 500 misses 1 s as well. Most of the extra append time is CPU on the invoking thread. Windows thread time advances in 15.625 ms ticks, and every NTFS append sample read 93.75 or 109.375 ms of it, against a 12.82–14.71 ms median on Linux. Each reading is accurate to one tick; the mean reading is 103.1 ms at 1,000 rows and 105.7 ms at 10,000, so most of an append of about 113 ms is on that thread. The tick is too coarse to resolve the remainder. Every stage grows, governance most (60.55 ms against 6.30 ms at 1,000 rows). Windows thread time includes kernel time, so file-system calls may account for part of it; that is not attributed. The one store commit and the lease's commit-generation fsync (3.70 and 4.34 ms) are the same as on Linux.

| Rows | Case | Store p50, ms | Files p50, ms | Files ÷ store |
| ---: | --- | ---: | ---: | ---: |
| 1,000 | `eq` filter | 170.86 | 5,582.47 | 32.7 |
| 1,000 | `contains` filter | 169.14 | 5,396.85 | 31.9 |
| 1,000 | date window, sorted, limit 50 | 181.53 | 5,396.66 | 29.7 |
| 1,000 | `count` | 167.75 | 5,332.07 | 31.8 |
| 10,000 | each of the four cases | 1,108.79–1,160.34 | not measured | — |

The answers match the Linux answers case by case. The query bound holds on NTFS at 1,000 rows. At 10,000 rows it is unmeasured: the shipped file path refuses that size, and the cap-lifted file run did not finish. The report SHA-256 prefixes are `ec6adb461b6ee558` (store) and `fd4773d8a918b536` (files).

The retained `measurement.json` SHA-256 prefixes are, before the fix (`36e95b57f`): store `35b7de2bf5d29a9a` and `6c9c78dd4aabbff5`, files `25c23f01d050612e` and `481b1d9661686995`; after it (`99f136bd8`): store `1a9bc9284bf9efbf` and `fad2ebe31a645a8f`, files `f92c0d6d985e06ed` and `99368d4e7a03c891`, each at 1,000 then 10,000 rows.

The spike numbers come from a 4-core container with ext4, SQLite 3.45.1, WAL and `synchronous=FULL` (`benchmarks/collections_sqlite_spike/results-*.json`). They exclude the dispatcher, the idempotency ledger, governance resolution and receipt projection.

**End-to-end append budget.** #1457's SQLite prototype confirms the storage layer is flat: 2.6–3.8 ms with synchronous projection at both 1,000 and 10,000 items. Its end-to-end estimate of about 93/100 ms p50/p95 is dominated by about 90 ms of shared cost outside storage, which that composite carried over unchanged from today's writer. That 90 ms is the real target of the 20 ms goal. The budget, per stage at p95:

| Stage | Today (#1457, after its fixes, 10 items) | Store design | How |
| --- | ---: | ---: | --- |
| Dispatcher, idempotency ledger, egress filter, digest | 7 ms | ≤ 5 ms | unchanged code; the ledger's fast-ack already sits inside it |
| Writer lease / mutation boundary enter and exit | ≈ 2 ms | ≤ 2 ms | unchanged |
| Collection resolution and manifest | 15 ms (78 ms before the parse cache) | ≤ 1 ms | a `collections` row lookup by name or id, plus a parsed-contract cache keyed by `(collection_id, manifest_version)`; no manifest discovery, file read or YAML parse on the write path |
| Request validation | ≈ 2 ms | ≤ 2 ms | unchanged; validators come from the cached contract |
| Governance precommit | ≈ 5 ms | ≤ 2 ms | policy resolved once (#1457 fix 1); uniform-release decision, or one identity-only pass (§7) |
| Store transaction plus item render and pending state | ≈ 60 ms of guard and write work | ≤ 4 ms | measured 2.6–3.8 ms (#1457 prototype), 0.7 ms without the view (this spike) |
| Post-commit fan-out: index sync and self-write registration | ≈ 50 ms (86–133 ms before) | 0 ms on the ack path | moved to the derived drain, since views are recall-excluded (§5) |
| Due-state and capture-sweep carriers | ≈ 3 ms | ≤ 2 ms | computed from the store row instead of an adapter re-read |
| Receipt projection | ≈ 1 ms | ≤ 1 ms | unchanged |
| **Total** | ≈ 155 ms p50 (157 ms at 10 items) | **≤ 19 ms p95** | |

Each stage gets its own timer in the acceptance harness, and the phase gate (task P1a.15) reports each stage against its budget. Under the 2026-10-19 owner ruling these budgets, like the 20 ms append target, are measured optimisation targets, not release blockers. This does not relax resource budgets or any integrity, authorization, recovery, compatibility or portability gate. The collection-resolution and fan-out rows are where the 90 ms goes. Both are removed by the store design itself, not by tuning.

The guard refresh a client performs between writes is also a command, so it pays the dispatcher. #1457 estimates 15–25 ms, against a storage cost of 0.04 ms. Two things make it rare and cheap:
- every mutation receipt already returns `after_container_hash` and `item_version`, so chained writes need no refresh;
- `record_memory(action="inspect")` retains its full public response and is measured as that command (target p95 < 15 ms). Its guard fields come from the `collections` row, but that lookup alone does not satisfy acceptance: current counts, observed-value frequencies, presentation and coverage remain part of inspection. When full inspection misses its target, the gate reports the miss; optimizing the remaining scans is optional work.

**Global inventory read reuse.** One global inspection owns one canonical read and authorization operation. Nested counts and coverage join that operation. Observation coverage reads the existing immutable parsed-file cache and shares one lazy canonical routing census across its retained manifests. Its batch-local closure cannot escape the call or authorize a later operation.

Keep canonical claims recomposition and per-row release checks. Every later public call refreshes policy, grants, session authority and canonical state, even when projection bytes stay unchanged. Atomic projection replacement invalidates the existing file token. File evidence remains the inventory's observations of current files, without a cross-file atomicity claim. Measure complete public latency and memory against parser-only reuse before claiming a gain.

**Full-inspection reuse (2026-10-01 refinement).** Start with pure terminal-egress computation: compute a canonical grant fingerprint only when an escalation notice needs it; reuse exact canonical receipt-dimension encodings within the invocation, preserving sorted-multiset digests and synchronous receipt emission; and memoize credential classification by exact string and structural/prose mode within one bounded scrub invocation. Issuance exceptions and withheld cross-checks remain outside that memo. Measure the existing full public command before adding aggregate machinery.

If inspection still needs contribution reuse, the existing writer handle owns it alongside A7's cache; no service, table, endpoint or schema is added. Extract the existing inspection contribution/finalization functions rather than implement a second formatter. A contribution contains only the currently released row's exact version/snapshot identity inputs, full stripped frequency strings, desired filename-group identity and row-local presentation inputs, qualified by canonical row and manifest/type/recipe identity. It retains no body, final response, escalation token or final authorization. Fresh operation authority selects the released subjects before loading or using their contributions; current grant additions/removals are operation-local overlays, never cached authority.

The contribution state and its derived counters/indexes have a **16 MiB retained-allocation budget across the writer handle**, in addition to the existing collection/subject/profile bounds. Account owned strings and containers conservatively, share contributions rather than copying values per audience, and reject admission before exceeding the budget. Overflow or an uncacheable collection uses the exact existing uncached inspection path: no truncated counter keys, top-values-only cache, reduced response or newly relaxed limit. Cold fills and eviction may cost a full pass; the budget does not promise every possible authored payload a warm path.

On budget pressure, compact dictionary tables once before choosing fallback. Reuse their existing keys and values, and adjust the retained accounting by the actual table-size differences. Rebuilding a table temporarily duplicates its table allocation, not its retained payloads. Fresh release selection, contribution identities and final filename-group recomputation remain unchanged.

Only committed identities can become dirty or reusable. Process dirty payload contributions on the next authorized inspection, not by rebuilding summaries on append's acknowledgement path. Recompute both old and new affected filename groups over the **final released set**: changing or withholding one row can change an unchanged sibling's finding. Keep inspection's existing `casefold()` grouping, distinct from portable-path normalization used to allocate append paths. Manifest/type/recipe changes, unexplained SQL, rollback, store replacement and eviction follow the existing invalidation boundary. Preserve full frequency keys, tie ordering, source-version bounds and exact visible-snapshot derivation; hidden-only changes cannot change a partial snapshot.

Compose audit/lifecycle guards, authorized held candidates, coverage, template/saved-view/link availability and projection progress freshly from their own dependencies. Future publisher/reconcile events must not leave stale projection findings behind a row-generation cache. The generic Records inspection gate also covers its existing Planning selector compatibility (§4); it does not widen `plan_memory`'s source-version, byte or JSON-node bounds or alter `PLAN_RESPONSE_TOO_LARGE`. Complete public output and terminal egress remain part of every measurement; these refinements do not establish the 15 ms target.

Removed from the acknowledgement path:
- the full collection read (≈ 4.3 ms per item);
- about 20 directory censuses and nine `lstat` guard rounds per item;
- manifest discovery and parsing;
- `log.md` read-rewrite;
- synchronous view index sync (≈ 50–133 ms), which moves to the derived drain.

**Global inventory reuse measurement, 2026-10-24.** Source `714568d8d` already contains the batch implementation and its serving-service regression. The existing regression and the synthetic measurement pass together: two pytest cases in 36.98 s. A dedicated canonical-reflector regression passes separately in 4.56 s. The cell uses a real loopback coordinator, mixed file/store collections, Linux/WSL2 ext4, Python 3.14.6 and SQLite 3.53.1. No product change was needed.

The same public `record_memory(action="inspect")` command runs at three collection counts. The baseline reintroduces one projection parse and routing closure per collection. The parser-only arm shares parsed projection bytes but retains one routing closure per collection. The batch arm uses the shipped implementation. All arms retain the current outer canonical authorization scope, so this ablation does not measure the older scope change. Every answer has equal committed, held and unreflected counts.

Each arm records one projection/parser-cold call, three warm calls and one untimed traced-allocation call. Cold clears those two caches; the service and SQLite remain running. The arms run sequentially in one cell without independent repetition. These are descriptive observations, not an installed or causal gain.

| Visible Records collections | Arm | Cache-cold latency, ms | Warm median, ms | Python traced peak per call, KiB |
| ---: | --- | ---: | ---: | ---: |
| 3 | Per-collection parse | 142.26 | 49.44 | 752.9 |
| 3 | Parser reuse only | 53.32 | 46.92 | 734.7 |
| 3 | Batch reuse | 55.10 | 44.34 | 1,373.9 |
| 6 | Per-collection parse | 151.60 | 114.13 | 773.7 |
| 6 | Parser reuse only | 122.28 | 120.00 | 743.8 |
| 6 | Batch reuse | 93.92 | 80.70 | 745.7 |
| 14 | Per-collection parse | 554.88 | 432.28 | 848.0 |
| 14 | Parser reuse only | 474.39 | 439.45 | 765.9 |
| 14 | Batch reuse | 184.05 | 162.41 | 774.3 |

At 3, 6 and 14 collections, the paired warm median differences for batch minus parser-only are −1.42, −28.49 and −282.86 ms. Their descriptive 95% bootstrap intervals are [−3.66, −0.11], [−43.53, −26.67] and [−287.15, −256.39] ms. The intervals resample only three paired positions and do not remove arm-order or host-load effects. Parser reuse alone has no established warm gain: its corresponding intervals all include zero.

At 14 visible collections, batching reduces the measured canonical claims censuses from 12 to one per public inventory. The projection parses once on the cache-cold call and zero times on warm calls. The pytest process lifetime peak RSS reaches 240.1 MiB; that is not per-arm memory. The traced peaks are Python allocation evidence, not family RSS. Existing latency targets remain optimization targets. Raw samples, counters, memory labels and limitations are in `docs/verification/collection-inventory-read-reuse.json`.

**Non-uniform collections and fsyncs.** The release-decision cache and the in-transaction `pending_sha256` (§16 A7) keep the budget for non-uniform collections and avoid a second fsync. The P1a.15 gate includes a 10% ref-withheld 10,000-row collection and a Windows/NTFS run. Measure that audience's `COLLECTION_NOT_FOUND` refusal separately from successful appends by an audience authorized for the complete collection; both paths retain the stage and p95 budgets. Partial visibility never authorizes a mutation (§7).

**Coordinated acknowledgement trace, 2026-10-20.** One guarded append ran through the service dispatcher and a real in-process SQLite coordinator on Linux/WSL. The isolated fixture kept views disabled. SQLite trace callbacks observed one store COMMIT and five coordinator COMMIT statements during the acknowledged operation. Four coordinator transactions changed no rows; one changed one row. Python `os.fsync` and `os.fdatasync` recorded no calls. The trace does not measure physical SQLite fsync calls or establish a latency distribution. The report SHA-256 is `3cc0c6a68483719a1490fc8f52dce998c0b8cbc4b60bbc5deedd4e890b023bec`.

The extra coordinator commits miss the acknowledgement target. The 2026-10-19 ruling makes that a reported optimisation miss, without relaxing durability. P1a.14c remains a non-blocking optimisation; the P3.6 real-sized copy still measures the public inventory read. P3.6 deployment, custody and real-sized acceptance remain separate prerequisites.

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
- Unknown keys, an unknown `kind`, a `placement` that collides with an existing layer or a reserved directory, a natural key naming undeclared fields, and an unreachable or undeclared state are all findings.
- **Occupied placement (ruled R10).** Declaring a type whose placement segment `Knowledge Base/<placement>/` already holds any file that is not a collection view is refused with `COLLECTION_TYPE_PLACEMENT_OCCUPIED`. A new type has no collection views yet, so in practice the folder must be absent or empty. The error names the folder and says to choose another placement or move the notes. That way rendered views never mix with hand-written notes, and edit-back never reads a note as a row. The refusal names only the folder the caller proposed, with no file names or counts, and it is identical whichever of those files the caller could read.
- **Later.** A Markdown file created afterwards under a type's placement is still never read as a row. Edit-back holds it as `VIEW_UNBOUND`, a proposed insert that is applied only by an explicit resume (§6).
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

The vocabulary grows only by shipped revision (ruled R9), because each kind needs a compiler mapping and version wording.

#### 14.3 Pinned version references (follow-up change, §16 A11)

*Deferred by ruling to a follow-up change; described here so the store schema (`item_versions`) keeps it possible.*


A `link` field may target a collection type and declare `pin: version`:
- its value is `exomem://<item_type>/<collection_id>/<item_key>@<row_version>`;
- an unpinned link, or `pin: current`, stores no version and resolves to the current row.

Resolution of a pinned link reads `item_versions`, which already hold every version. The pinned target is authorized as the item itself (the same governance subject), so a withheld target projects to `None`, exactly as `_LinkProjector` does today. Write-time validation checks that the pinned version exists and was authorized for the writer. The natural key and `payload_hash` include the full pinned value, so two runs that followed different revisions are different observations. Query `group` and `filters` may use the pinned value, or its two parts: `<field>.item` and `<field>.version`.

#### 14.4 Surfacing: when a turn is served a type's items (compiler-lane rewrite deferred, §16 A11)

*The generic `collections` lane, `collection_kinds` and `item` anchors below are deferred by ruling to the same follow-up change. In this change a declaration's `surfacing` block is validated and stored, and types surface only as today's Records (`current_state`) and Planning (`active_plans`) do.*


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
3. The very next call can add items (14.7). The type's placement is registered as a structured layer, so its views are excluded from ordinary recall like `Records` and `Planning` (`recall_policy`) and are pinned for the current command surfaces. Both read the type registry instead of hard-coded layer names.

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

The leaf API is generic: `collections.ops.{describe, validate, inspect, create, query, add, update, transition, revise, rebaseline, bulk_upsert, hold, resume, discard}`. It takes a collection and resolves its type. `transition` is the generic lifecycle move: `item_key`, `expected_item_version`, `why`, and `changes` naming the target state as `{<state field>: <state>}`, optionally with other field changes. It is checked against the declared state machine, per-state constraints and named validators.

The two existing tools become **facades**, which are declarative maps held in the built-in declarations' `wire` blocks:
- **`record_memory` → type `records`.** Actions map one to one (`append` → `add`). The receipt, error codes, argument sets and describe text are unchanged.
- **`plan_memory` → type `planning`.** `add` → `add`, `update` → `update`, `triage` → `transition` plus field changes, and `revise`/`rebaseline`/`inspect`/`query` → generic. Receipts, `STALE_PLAN_*` / `PLAN_ID_CONFLICT` codes and the exact inspect shape are unchanged. The Planning hierarchy is the `planning.hierarchy.v1` validator.

The facades refuse a collection of the other built-in type (today's boundary scenarios hold).

**Declared-type items (ruled R8): `record_memory` serves them; no new tool.** `record_memory` accepts any collection whose type is neither `planning` nor another facade's type. For declared types:
- it uses generic names (`item_key`, receipt marker `_collection_receipt`, generic error codes);
- it gains `action: "transition"`;
- `describe` takes an optional `collection_type` and teaches that type from its declaration.

The retired Hosted profiles are unchanged: the new action and parameter appear only on the local surface and the v5 candidate.

**Schema-byte budget.** The tool-description diet is cutting schema bytes, so the added surface is minimal. There are exactly two additions:
- one enum value (`transition`) on the existing `action`;
- one optional string parameter (`collection_type`) with a one-line description.

`transition` reuses the existing `item_key`, `expected_item_version` and `why` parameters, plus the existing `changes` for the target state, as `{<state field>: <state>}`. It adds no `to_state` parameter. The tool description gains at most one sentence. Everything a declared type needs (its fields, states, transitions, examples) is taught on demand by `describe(collection_type=…)` from the declaration, never carried in the static schema. The generated tool schema for `record_memory` may grow by at most 400 bytes, measured and gated in task P5.3.

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

**3. Log executions in a Records collection.** *(The pinned link and step 4's surfacing are delivered by the follow-up change, §16 A11. In this change the link is unpinned.)* "Recipe Executions" is an ordinary collection of the built-in `records` type (`kind: observed`):

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
- The exposure is the replica publish interval for a crash between commit and publication, at most 60 s under steady writes (§16 A9). It is flushed synchronously at every orderly boundary. Operators who need zero window use `exomem collections backup --stdout` as the restic source.

**4. Obsidian visibility and edits.** Views are not read-only in effect. The owner chose governed edit-back (option 2): a valid edit becomes an audited update, and an invalid, ambiguous or conflicting one becomes a held correction. An edit is never silently overwritten by the next projection, because edit-back runs on the changed file before any re-render, and a stale-base edit is held with the human's bytes (§6).

**5. Sync tools and two machines writing.** This is the one place the store design is genuinely weaker than files. Today two machines writing different items merge through ordinary file sync, and a same-file conflict becomes a sync conflict copy.
- **With the multi-host writer lease** (the supported multi-writer setup), only the lease holder writes the store. Edits made on another machine are view edits. They reach the writer's vault through sync and go through edit-back there, so no store merge is ever needed (§1, §6). Sync conflict copies of views are held `VIEW_CONFLICT_COPY`.
- **Without the lease**, two services each writing their own live store on one synced vault diverge. That includes a Windows and a WSL service on one vault, which have different state roots and locks. Instance identity, replica check-then-swap and stamps (§16 A4) detect it immediately. Collection writes then fail closed with `COLLECTION_STORE_DIVERGED`, while reads and knowledge writes continue.
- **Nothing is lost.** An operator command, `maintain_memory(mode="collections-store-reconcile", dry_run=...)`, takes the divergent store's transactions after the fork point. It turns every changed item into a held view correction on the surviving store, carrying that store's values and diagnostics, and records the reconciliation as one content-free transaction. A human then resolves each one, as with a sync conflict copy today.
- Running two writers without the lease becomes explicitly unsupported for collection writes. `describe` and the doctor probe say so. Knowledge Markdown is unaffected.

**6. Out-of-band edit policy.** This is a product decision the file design does not need, and the owner has made it: ingest through the governed write path, or hold (§6). Detection is exact and does not depend on the watcher. `projection_state.published_sha256` / `pending_sha256` identify the design's own writes, and every view carries a stamp naming the store, instance and row version it was rendered from (§16 A1, A2), so a stale or foreign edit can never pass as a current one.

**7. Everything else that reads items as pages; read-your-writes.**
- **SQLite commit is the acknowledgement boundary** (§5). Item and manifest files publish asynchronously alongside aggregate views. Structured reads stay current. `get_page` keeps its current governed physical-view validation and can remain unavailable during publication lag.
- **Structured readers move to the store** (task P1a.12): due-state, plan progress, capture sweeps, the working set and current-state resolution, `audit` outcome bindings, and evidence bundles built from items. They are read-your-writes by construction and no longer parse Markdown.
- **Internal delta truth is not disclosure (P1a.12 preservation ruling, 2026-10-01).** Due-state's existing write-delta projection remains audience-independent. In dark preview its private snapshot comes from the already trusted lease-owned writer binding, with opening-thread and operation-context integrity checks; it is not a public adapter option or a substitute principal. Public readers continue authorizing canonical row identities before decoding values. At serve, joined canonical rows are released under the reading principal and withheld rows are indistinguishable from absent rows; missing or stale Markdown views and whole-log file grants supply no store authority. Reuse the ordinary snapshot materialization without introducing another evaluator, privilege-switching context or publisher.
- **`find` and recall** already exclude Records and Planning descendants except manifests (`recall_policy`). Declared types join that exclusion (§14.5). Lexical, resolver and graph indexing of views therefore lagging the drain does not change any recall answer about item values.
- **Plan links, supersession and pinned references** resolve through the store (§14.3), not through view files.

**8. Cloud cells.**
- One writer lease per cell makes SQLite a natural fit, and the live store sits in the cell state root on the tenant volume.
- Cell export and restore gain the store snapshot as canonical data: a single-file backup-API copy, never the live store's files (§11; the `hosted-vault-portability` delta). Restore stages, validates and re-renders before publication.
- `cloud_import` of a file-canonical vault runs the §10 importer and proof in staging before the cell starts.
- Evidence bundles read rows.

**9. Governance and withheld = absent, and per-row cost.**
- Each row keeps its view path as its governance subject, along with its ref, type, tags and project (§7).
- Per-row evaluation is avoided in the common case by the uniform-release decision: one decision when no scope in the resolved policy can distinguish the collection's rows (0.4 ms at 10,000 in the spike).
- When rows are distinguishable, the per-row pass evaluates identity-only columns against a once-resolved policy (44 ms at 10,000 in the spike). Where the distinguishing selectors are path prefixes, refs or declared tags, they compile into a SQL predicate with a tombstone join, and the pass runs in the database. Other selectors fall back to the in-memory pass. Both are held to the parity test.
- Audit rows are redacted by the same subject rules (history pages §5; `inspect` history §7).

**10. Deletion and trash.** Records and Planning have no item deletion today, and this change adds none.
- The file tools (`delete_file`, `move_file`, `delete_directory`, `recover_from_trash`) refuse on paths that admitted canonical ownership gives a store collection, with `COLLECTION_VIEW_PATH` and a remediation naming `record_memory` or `plan_memory`.
  - Canonical ownership regions (manifest, source, held and history) contain every `projection_state` path. They also cover an unbound file, a folder that holds a view, and a move or restore destination inside a collection.
  - Proved file-only vaults retain file behavior. Unavailable ownership delays ambiguous collection-layer paths while provably disjoint knowledge stays usable.
  - Each tool decides its withheld-as-missing check first, so a withheld view answers exactly as a missing path. The destination check runs before any destination existence check.
- If a view is removed outside Exomem, the deletion is held as `VIEW_DELETED` and the view is re-rendered.
- The trash never holds canonical collection data, so trash recovery has nothing to restore for collections.

**11. Exactly-once.** #1457 notes that exactly-once already comes from the idempotency ledger, independent of storage. The store adds the one window the ledger cannot close: a commit before the ledger records it. It does so with `txns.request_id` and the receipt recorded in the same transaction (§4).

**12. Recommendation for the middle path (a derived SQLite sidecar with files authoritative).** It is superseded by the owner's decision. Its revisit conditions are now met, because bulk upsert (#1452) and declarable collection types (§14) make multi-row transactions and larger collections first-class. The sidecar's benefits (O(1) guard, O(1) natural-key lookup) are the store's primary indexes, and the stat-generation item cache that #1457 added (`record_item_cache.py`) becomes unnecessary for store collections. It stays useful for file mode during the legacy window (R7).

### 16. Safety amendments (critic review, ruled)

An independent review returned AMEND FIRST. The core decision stands. The amendments below are ruled, and they change the P1a schema. Where earlier sections disagree, this section wins, and those sections have been edited to point here.

#### A1. Publishing never overwrites an unseen edit (check-then-swap)

Earlier, §5 renamed staging over a view and §6 relied on the watcher to have seen any edit first. The watcher can miss one:
- `watchdog` is optional, and the watcher is a no-op without it (`file_watcher.py:16-18`);
- the reconcile interval is 300 s (`:77`);
- the settle windows are 2 s and 10 s;
- the service may be offline.

An agent write landing in any of those gaps would rename staging over a human's edit, record the new `rendered_sha256`, and lose the edit.

**Every publish is check-then-swap.** This covers asynchronous item, manifest and aggregate views and synchronous held views alike:
1. Atomically rename the current view to a target-adjacent aside name (`.<name>.exomem-aside-<txn>`).
2. Install staging at the view path with **no-clobber** semantics: `link` then unlink staging on POSIX, `MoveFileEx` without `REPLACE_EXISTING` on Windows (`renameat2(RENAME_NOREPLACE)` where available). A file that an editor created in the gap between the two renames is therefore never replaced. The install fails, and that file is treated exactly like a changed aside (step 4).
3. Hash the aside copy.
4. Compare the hash:
   - If it equals the view's expected on-disk hash (`published_sha256`, or `pending_sha256` when a previous install completed without being recorded, see A7), delete the aside.
   - Otherwise the aside is an unseen human edit. It is held as `VIEW_CONFLICT` carrying its bytes, its stamp (A2) and both row versions, and removed from the view path. The newly installed view reflects the store. The human's bytes are never lost.
5. A rename that is blocked (a Windows open-file lock) leaves the view `pending` and is retried. It never falls back to an overwrite.

**Startup and periodic reconcile.** Before the projector runs, on every start and every 60 s, the service hashes every `projection_state` path and feeds any mismatch to edit-back classification (A2). Views whose on-disk hash equals `published_sha256` are skipped by an `(inode, size, mtime_ns)` pre-check, so a quiet vault costs one `stat` per view. This does not depend on `watchdog`, so a missing watcher only delays edit-back, never loses an edit.

#### A2. Every view and log block carries a stamp

Earlier, §5 rendered only the identity properties and §6 took the edit's base from `projection_state`. So a stale Obsidian buffer or a second device writing v4-based bytes over v5 would classify as a valid current-base edit and silently revert v5 under `owner:view-edit`. The same held for log blocks, and a forked store's views would be adopted as owner edits. The spike already had the right shape (`spike.py:379-383`).

**The stamp.** Every item and manifest view renders one system property:

```yaml
exomem_view: {s: <store_id>, i: <instance_id>, v: <row_version>, h: <payload_hash[:12]>}
```

Each log block carries the same stamp as an HTML comment (`<!-- exomem-view s=… i=… v=… h=… -->`), replacing today's `exomem-record-audit` marker line. The stamp is a reserved system property: it is refused in `item` and `changes`, and ignored by the payload hash. `instance_id` is the physical store instance that rendered the view (A4).

**Classification by stamp**, ahead of every other row in §6:

| Stamp on the edited view | Result |
| --- | --- |
| `s` is not this store's `store_id`, or `i` is not in this store's instance lineage (A4) | foreign view → `COLLECTION_STORE_DIVERGED` (A4); the file is held `VIEW_FOREIGN`, never applied |
| `v` < the row's current `row_version` | stale base → held `VIEW_CONFLICT`, carrying both versions and the human's bytes |
| `v` = current and `h` matches the row | current base → the ordinary §6 classification (formatting-only, governed update, invalid → held) |
| `v` = current but `h` does not match the row | a stamp that was tampered with or copied → held `VIEW_INVALID` |
| no stamp, and the bytes are exactly the file parsed at import (A5) | a legacy view that has not been re-rendered yet; its base is the import version |
| no stamp, otherwise | held `VIEW_INVALID` (a missing stamp is never read as the current base) |

Only imported legacy files may be unstamped. The first re-render stamps them. Log views are diffed block by block with the same rules per block.

**First publication and exact rollback.** Migration retains exact item and log source bytes in versioned, namespaced `store_meta` records. The temporary import-material proof covers those bytes. Immutable manifest versions and held-byte rows already retain their originals. Each archive record binds to the collection, item identity, import checkpoint and source digest. Control metadata reads omit archive payloads; SQLite snapshots still copy the complete database.

After admission, the serving census queues matching imported item, manifest and log views for their first stamped render. Imported bytes remain the published baseline until normal publication confirms the canonical target. An offline rename still requires a completed census, one matching identity, the original digest and the existing path guards. The archive follows immutable item identity; it never grants egress authority to unstamped bytes.

An older admitted import captures only source bytes that still match its import proof. Admission verifies that the replica carries those originals before stamping. A same-head backfill forces the existing copy-and-swap path when needed; interrupted admission checks again. Missing original bytes remain unresolved rather than becoming reconstructed or fabricated originals.

Automatic stamping creates no native mutation. Reverse export restores exact originals for a collection with no native mutations, even after its views were stamped. A governed `view_move` remains a native mutation and keeps the existing export checkpoint rules. Archive records remain in snapshots and export tombstones. A supported undo release must retain archive-aware export; schema-13 compatibility alone does not prove a safe undo.

#### A3. Cross-host freshness uses a coordinator-recorded store head

Earlier, §1 checked freshness against the synced replica only. Idle release after 60 s (`writer_lease.py:1726-1731`) makes handoffs routine, so a new holder could write on a replica that sync had not yet caught up, forking the store. The rule "local head appears in replica" was also undefined: `txn_id` integers collide across forks, and the hash chain is per collection.

**Store-wide chained head.** Each transaction also advances a store-wide chain:
- `txns.store_head_hash = sha256("exomem-store-head:v1\0" + prev_store_head_hash + "\0" + commit_seq + "\0" + event_hash)`;
- `store_meta` keeps `commit_seq` and `store_head_hash`.

A fork is exactly "same `commit_seq`, different head", or "my head is not an ancestor of yours". Ancestry is decided by walking `txns.store_head_hash`, never by comparing `txn_id`.

**The coordinator records the head.** The lease holder sends `collection_store_head: {store_id, instance_id, commit_seq, head_hash}` on every renew and ordinary release. This is coordination metadata only, which the "Provider-neutral coordinator contract" allows. The coordinator persists it with the valid holder/token operation and retains it after release or expiry; stale holders cannot replace it. Operator `release_holder` preserves the existing head rather than supplying the operator host's head. Head validation uses canonical UUIDs, a nonnegative sequence and a null genesis head or 64-hex non-genesis hash. A separate vault-specific `collections-store-v1` capability fence admits store-aware holders (A5); governance schema versions 3/4 remain unchanged.

**Thread and release binding.** The renewer reads through a narrow trusted head provider, never the opening-thread SQLite writer. Every ordinary release path flushes the replica while local writer authority remains valid, then reports the head and clears authority. This includes idle release, whose current file-only implementation clears its token before its release RPC. No new scheduler or second policy evaluator is introduced.

**One vault-bound runtime adapter.** The real store operation path and lease lifecycle share one adapter in `collection_store/runtime.py`, bound to the exact vault, store and instance. Admission rechecks mode/adoption, the applicable capability fence and verified publication custody; transport support alone supplies none of those. The head provider opens and closes a fresh read-only connection on its calling thread and reads the identity and committed head from one SQLite snapshot. It reports the live committed head, never a cached last-published head. An enrolled store that is missing, unreadable or inconsistently bound is an error, not an absent head.

**Retain the warm handle; retire it at handoff.** Ordinary operations reuse the opening-thread-owned writer and its A7 cache. The adapter brackets every use, including read snapshots and standalone use, with a scoped checkout. Connections, cursors and request bindings cannot escape that scope. Closing each request would discard A7 reuse.

Before handoff, the adapter stops new checkouts and write admission. It drains borrowers and authority holders without holding the manager lock or canonical mutation boundary. After drainage, it acquires that boundary, revalidates the exact token without reacquiring, and detaches the idle handle. It verifies that no transaction remains, then closes the handle. Orderly flush, admission and direct writers retain their existing opening-thread lifetime.

**Lend only for coalesced publication.** Each short coalesced publication step may borrow the matching cached writer inside the existing publication window. That window excludes borrowers and mutations. The handle must be open, bound to the same path and token, and outside both SQLite and SQLAlchemy transactions. The step temporarily owns the handle and restores its prior owner before releasing the window, including after failure. Outside copying, hashing and flushing retain no writer ownership.

An unfinished transaction defers publication; an unusable handle cannot return to the cache. No eligible cached handle means the existing retire-and-open path applies.

This exception preserves the same connection's external-change witness and bounded cache through publication metadata commits. Publication accounts each known metadata statement through the existing managed-write mechanism. It never accounts an arbitrary callback, step or whole transaction. Unexplained changes, external commits, schema changes and rollback retain their existing invalidation behavior. Ordinary foreign-thread calls and copied preview contexts still refuse. No owner-thread actor, dispatch protocol, connection, schema or authority cache is added.

The existing SQLite configuration permits sequential cross-thread access. [SQLAlchemy's connection contract](https://docs.sqlalchemy.org/en/21/core/connections.html#sqlalchemy.engine.Connection) requires synchronized access and compatible driver support. The publication window supplies that exclusion; the exception grants no concurrent use. These dependency contracts do not replace the integration proof.

Warm checkout validates the held token through the existing coordinator status admission. It reacquires only when the token is absent or fenced, before lending the handle. Checkout acquisition does not count as write activity. Mutation and publication authority checks never reacquire after work starts. Every coordinator fence change (store cut, store retirement, schema cut) revokes the holder and advances the fencing token in the same transaction. A collection-store write nested inside an operation therefore reuses the operation's grant while its token is current and the grant is younger than the lease lifetime less one renewal interval, because the store transaction revalidates the token with the coordinator before it commits. Nested file guards acquire as before: their file effects have no such check, so each of them keeps the fresh coordinator grant that refuses a revoked holder.

**Publication asks the coordinator before each effect.** Each publication step starts with a coordinator status read under the boundary. After that, the publisher's authority callback asks the coordinator again before every rename, unlink, and directory create or removal. It also asks inside every bookkeeping transaction, before the statements and the commit, and the transaction asks as it begins. The writer's own guard repeats the callback's evaluation in the same check, so the guard trusts the held grant while the grant is fresh. A grant is fresh while it is younger, on this host's monotonic clock, than the lease lifetime less one renewal interval, and no renewal has failed since it arrived. Nested store writes use the same rule. A progress check is a check after which the publisher changes nothing on disk except a flush before its next check. It trusts a fresh grant the same way. The entries of an orderly run, a prepare and a swap, and the staging cancellation check, are not progress checks: a filesystem capability probe or stage files can follow them. A revoked or expired lease therefore stops publication before its next effect. A host suspend does not age the grant, so a progress check can pass after the lease expired; the next effect check refuses. Each step also resolves vault placement once, after it holds the boundary, as a request does.

**Ordered reporting and renewal during flush.** One report lock serializes exact-token check, fresh head sample, renew/release RPC and local result update, so an older concurrent sample cannot overwrite a newer reported head. No report-lock holder acquires the canonical boundary or calls publication, and no caller waits for that lock while holding the manager lock. Release-start renewal, due renewal from publication authority/progress checks and final release each use that operation independently; publication runs outside the report lock. Renewal remains available until flush and release finish, including when idle release runs on the renewer itself. The final release carries the verified flushed head while the mutation boundary is still held, and only then clears local authority.

**Failure is not a completed handoff.** Busy-boundary acquisition, cancellation, deadline expiry or a failed flush defer idle release. Explicit shutdown reports an unsuccessful or pending flush rather than claiming replica handoff; lease loss stops publication without implicit reacquisition. Forced termination leaves recoverable intent and normal lease-expiry semantics. Cooperative renewal/deadline checks do not promise a hard bound on blocking OS I/O. Repeated close and the atexit fallback are idempotent; atexit registration alone is not orderly-flush evidence. This ordering prevents acknowledged rows being handed off ahead of their replica; a false busy/deadline result costs a deferred idle handoff, not a new owner approval or a permanent vault-wide refusal. File-only lifecycle behavior remains unchanged.

**What the new holder does:**
- If the recorded head came from **another instance**, which is a real cross-host handoff, the new holder compares it with its vault replica.
  - Replica at the recorded `(commit_seq, head_hash)`: adopt the replica, then write.
  - Replica behind: collection writes refuse with the retryable **`COLLECTION_STORE_SYNC_PENDING`**, naming the recorded and local `commit_seq` and saying what to do: "wait for vault sync to deliver `Knowledge Base/_Collections/collections.sqlite`, or run `exomem collections adopt-local` to continue from this host's copy".
  - Replica at the same `commit_seq` with a different head: `COLLECTION_STORE_DIVERGED` (A4).
- If **no foreign head was ever recorded**, the new holder never waits. That covers a single-host vault, the same instance re-acquiring after idle release, and every non-multi-host install.
- **Unchanged re-admission copies nothing.** The same instance re-admits without a new lease epoch or a replica copy when all of these hold:
  - the coordinator recorded this instance's live head, and the live store needs no schema migration;
  - the store published that head under its stored epoch, with no pending publication, create intent, divergence or foreign adoption record;
  - the vault replica holds exactly the bytes the store last published.
  The replica hash runs before the mutation boundary. Under the boundary, settlement compares the publication records and the replica's file signature, change time included, again; any difference, or a record it cannot read, takes the full publication path. Custody, the import-original backfill, the token check under the report lock and the head report still run. The stored epoch is a grant that every later token exceeds, so another host adopts the replica as before. This keeps the routine idle-release cycle from holding the boundary for a whole-replica copy.
- **Bounded.** The refusal is re-checked on every replica change (the `_Collections/` watch, A4) and every 10 s. After 15 minutes it also raises attention with the same remedy. Reads and knowledge writes are never blocked.

**Explicit adopt-local.** `exomem collections adopt-local --why "…"` (also `maintain_memory(mode="collections-store-adopt-local")`) is owner-only and preview-first. It continues from the local store deliberately:
- it records a fork point, meaning the foreign head it is not waiting for, in `store_meta.forks`;
- it adds a lineage entry;
- it proceeds.

If the other side's transactions later arrive in a replica, they are reconciled exactly like divergence (§15 item 5): every item they changed after the fork point becomes a held correction. So a switched-off sync tool can never lock the owner out, and choosing to go ahead never silently drops the other side.

#### A4. Instance identity and immediate divergence detection

Earlier, divergence was not always detected. The state directory is keyed on the resolved vault path (`state_paths.py:260-275`), and mutation locks live under the state root (`mutation_lock.py:1936`). So a Windows service and a WSL service on one vault, or a moved vault, get separate live stores and separate locks, and fork silently.

- **Instance identity.** Each live store mints an `instance_id` when it is created or adopted. `store_meta.lineage` records `(instance_id, adopted_from_instance_id, adopted_at_commit_seq, head_hash)`. A stamp or replica from an instance in the lineage, at or before its handoff point, is this store's own history. Any other instance is foreign.
- **Replica check-then-swap.** Before publishing, the publisher hashes the replica on disk and compares it with `store_meta.last_published_replica_sha256`, the last replica **this instance** published. A mismatch means another instance published. The publisher then does not overwrite: it marks the store `DIVERGED` and keeps the foreign replica aside as `.foreign-<instance>`. The swap uses the no-clobber install from A1.
- **Recoverable publication intent.** The coalesced publisher uses one versioned pending record with `staging` and `ready` phases. It commits the operation token, derived workspace/stage names and store/instance/lease binding before workspace allocation, then records the exclusively created workspace's held identity before creating any snapshot bytes. A staging intent has no promotable digest or head. Once staging passes validation and durability checks, the same record becomes ready with the exact snapshot digest and business sequence/head. Recovery first resolves this record: an absent staging workspace clears the intent; a matching owned workspace permits cleanup of only its recorded scratch family; an unrecorded empty workspace may be claimed, but unexpected contents or changed identity remain unresolved and preserved. Ready recovery recognizes its installed digest, its exact predecessor and an interrupted displacement. An unexpected shared target or captured predecessor is preserved and marks divergence; cancellation, deadline expiry, lost authority and sharing failures remain retryable. Missing-stage recovery restores the verified predecessor without clobbering a new file before restaging. The actual verified installed file and its directory are flushed before digest/head promotion or a successful synchronous-flush result; identical bytes on a new inode do not inherit an earlier file flush. Bookkeeping never advances the business sequence and stays off the acknowledgement path.
- **Private publication custody.** Shared replica names are external inputs. Staging and predecessor retirement use one exclusively owned, sync-excluded workspace beneath the replica parent on the same volume. The publisher atomically captures the shared predecessor into that workspace before classifying its actual bytes; it never unlinks a shared publication name. Destructive cleanup occurs only inside private custody, never by repeating a hash/check before shared-directory unlink. A file arriving at the shared destination is preserved by the no-clobber install. The supported guarantee covers concurrent shared-name inputs, not hostile same-user writes into private custody or the canonical store. The workspace and its contents exist only for the active operation or bounded recovery; foreign evidence is deliberately retained. This costs one temporary directory and off-ack metadata commits, not an indefinitely retained full snapshot per publication.
- **Each name change is durable before the next.** After the capture rename, the install rename and the workspace removal, the publisher flushes the directories that changed before it changes another name or promotes the head. Otherwise a crash could keep the later unlink of the displaced replica and lose the rename that installed its successor. A held filesystem whose rename and directory unlink already flush the parents they change declares it (`HeldFilesystem.name_changes_flush_parents`), and the publisher does not repeat that flush. The POSIX implementation declares it; the Windows one flushes explicitly.
- **Dark publisher integration seam.** Until the single mode/adoption/fence adapter is implemented, internal publish/recover calls require an explicit trusted authority callback with no permissive default, plus intrinsic vault/store binding, exclusive thread ownership (A3), open-handle and current lease checks. The caller retains the vault mutation boundary throughout the operation and supplies exclusive private workspace custody. For a synced deployment the future adapter must verify incoming and outgoing subtree exclusion for every sync client touching the vault; a random name, permissions, a reserved descriptor or the writer lease alone does not prove it. Isolated local callers establish custody through their own disposable directories. Missing custody prevents successful replica publication, not unrelated reads or knowledge writes; the deployment owner pays that bounded failure cost. No human attestation flag or new policy evaluator is added. This foundation has no public or service caller and does not establish marker, takeover or S1 acceptance.
- **Foreign preservation follows the durable fence.** Record divergence before moving the unexpected replica to its owned no-clobber `.foreign-*` location. This filesystem-only preservation retains all mode, identity, exclusive thread ownership (A3), open-handle and live-lease checks; it never permits a business transaction through the divergence fence. If cancellation, authority loss or sharing prevents the move, keep the bytes in place and report preservation pending. Publication does not resume, and owner reconciliation must retain that input before replacing it.
- **Watching `_Collections/`.** The directory is watched, and polled by the A1 reconcile. Any foreign replica, or any view stamp from a foreign instance, sets `COLLECTION_STORE_DIVERGED` immediately: collection writes refuse, and reads and knowledge writes continue. Reconciliation is the §15 item 5 command.
- **Moved vaults.** Opening the same store from a new resolved path (same `store_id` in the mode marker, A5; no local store at the new key) is an adoption, and gets a new lineage entry. It is not a fork.

#### A5. Canonical ownership and fenced compatibility hints

**Supported-client boundary (owner, 2026-10-04).** Older-reader/startup exclusion in this change covers supported Exomem launchers and candidate admission, not arbitrary historical executables launched manually. Before fresh external-state bootstrap, candidate admission or copied/restored-vault activation, the launcher derives required store compatibility from trusted production enrollment and verified store or restore metadata. It uses the existing candidate-bound support probe and supervisor stop/handoff. An external descriptor alone cannot fence a reader with a fresh state root. No additional in-vault sentinel, physical state family or routing authority is introduced.

Sync custody covers explicitly supported deployment adapters and their participating endpoints. A single-host/no-sync adapter automatically checks that its configured supported sync roots do not overlap the vault. A synced adapter verifies effective incoming and outgoing private-workspace exclusion at every participating endpoint and establishes actual marker-write exclusion/quiescence before replacement. One ignore file, a caller boolean or pausing only the initiating publisher is not proof. Unknown or lost custody leaves store activation/publication pending, preserves intent and conflicting bytes, and does not disable unrelated file collections or knowledge access. This prevents lost routing updates and deletion of incoming data; a false refusal delays the affected owner's store operation. No per-operation human attestation is required. All-client custody obligations elsewhere in this change mean all participating clients within this supported deployment boundary.

**Single-host coordination.** A managed service may explicitly select `EXOMEM_WRITER_LEASE_TOPOLOGY=single-host` and use the shipped coordinator on a credential-free literal loopback HTTP origin. The client rejects other URL components, bypasses proxies, and keeps redirects within that origin. The closed topology enum selects an implemented deployment boundary; it is not vault vocabulary. Omission retains `multi-host` and its shared OAuth requirement. With no shared storage URL, single-host retains the existing local SessionAuthority and FastMCP OAuth storage, signing key, and identity. An explicit shared storage URL retains its existing selection and validation. Doctor retains local coordinator and lease checks without HA edge, shared-auth, or two-replica requirements. The existing fence, head, mutation lock, governance, supported-reader admission, and deadlines remain in force. This boundary prevents remote coordination from silently retaining local identities; a false refusal delays that owner's setup. It adds no protocol or session migration.

**Custody evidence.** An enabled built-in sync plugin is a capability bit, not a connection or sync root. The shipped registry does not infer root overlap from that bit. The bounded adapter still checks configured supported roots, root metadata, platform paths, and vault/state separation on every lifecycle recheck. It reads core-plugin configuration only when the registry supplies that evidence kind; an explicit owner overlay retains its conservative refusal. No boolean override or declaration grants custody. Community-plugin evidence remains unchanged.

Earlier, §10 stored the mode twice with no ordering, adopted a replica whenever no local store existed regardless of mode, and left the replica behind after reverse export. Old-release hosts could keep writing files that the store would then read as view edits.

- **Canonical ownership and marker hints.** Derive the complete ownership set from admitted collections, current manifests and transition intent. Validate that set before principal filters or discovery limits. The existing mixed marker retains its format as a compatibility hint and transition preimage. Its absence or older membership cannot return canonical collections to files. Adoption requires trusted production enrollment, the current fence and a verified snapshot binding. Reverse export retains the existing verified intent and tombstone protocol.
- **Prove each sample complete.** Before trusting file authority, require the sampled SQL chain to contain the coordinator's recorded head within the same store and valid lineage. Use the existing status response instead of the fence read on the normal recorded-head path. A genuine null head retains the fence read and existing first-create rules; a failed response never becomes null. After export retirement, use the matching retired-fence head until completed retirement is proved. Keep these heads out of the before/after ownership binding. Each sample independently proves containment, so harmless row writes can advance both heads.

  This prevents stale recovery from treating a later collection's view as an ordinary file. A wrong refusal delays relevant reads until retry or sufficient state; disjoint knowledge stays usable. Normal samples retain one authority RPC; null-head bootstrap needs another fence read. Status briefly uses the coordinator's existing immediate transaction, which can add contention.
- **Read recovery facts before admission.** The admission path reads complete declarations as structural facts before its existing acquisition and takeover decisions. These facts confer neither ordinary file authority nor canonical row admission. Read pending intent and declarations in the same SQL snapshot. Public ownership proofs retain their complete-state floor.
- **Validate ownership at both ends.** Capture the complete binding under the consistency boundary before a read relies on it. Release the boundary for computation and provider work. Compare a fresh complete binding before final result or error release and before its disclosure receipt. If relevant authority changed or became unresolved, withhold the whole payload through the existing unavailable outcome. Compare placement, enrollment, store and instance identity, fence generation, ownership and transition admission. Ordinary heads, row versions and publication hashes do not change ownership. Disjoint knowledge does not require irrelevant SQL. This replaces the earlier body-long borrower proposal; process-local pins cannot protect separate CLI producers.
- **Bound ownership work by declarations.** Keep full stored-path containment checks at admission, import and restore, using all canonical declarations. Routine proofs do not enumerate item, held or projection paths. Supported writers keep generated paths inside immutable declaration regions; selected reads and publication still check their exact path binding.
- **Keep preview admission separate.** Without trusted active enrollment or verified restore, the first admission may include only its complete verified intent cohort. Refuse unrelated preview rows before preparation or enrollment. Preserve recovery of a valid first-create intent without adding a catalog or flag.
- **Bind export to a complete hint.** Plan every canonical collection. After validating that plan, repair an absent or incomplete compatibility hint through the existing guarded preimage replacement. Record the unchanged export intent against the resulting exact bytes. Preserve conflicting or foreign hints. A crash before intent creation leaves only a harmless hint repair; re-preview may yield a new plan identity.
- **Retire generated history during export.** File-mode readers cannot retain the store's per-effect and field policy checks. Include registered history views as absent targets in the existing export. Remove only exact recorded generated bytes, verify absence before fence retirement, and preserve every audit row in SQL tombstones. Unknown or edited history blocks export. Keep projection bindings during partial export; abandon queues missing history for normal regeneration without rewriting collection file views. Its offline close skips only view drainage; replica publication, checkpointing and verified head release remain mandatory. After verified fence retirement, finish renames without removing newly authored history-path files.
- **Bound the retry cost.** A concurrent namespace change can discard one response and its computation. Whole-vault aggregates can lose work when another collection changes. This rare refusal prevents stale file authority without serializing ordinary writes behind long reads. No new durable epoch, catalog, cache or capture-provenance framework is needed.
- **Contain unresolved ownership.** Derive possible collection layers from existing profile placement. Without complete admitted state, refuse ambiguous file fallback inside those layers and preserve disjoint knowledge access. This prevents file tools from overwriting canonical views; a false refusal delays ordinary files in a possible collection layer. With complete state, proven ordinary files remain usable.
- **Distinguish active enrollment from residue.** Trusted production enrollment and verified restore metadata require store recovery even without a marker. A completed export and verified retired fence override residual enrollment. A preview database or failed dark preparation cannot enroll a file-only vault.
- **Use the first schema-13 boundary.** Deliver repaired ownership with the pending installation semantics in the first schema-13 release. Record schema requirements before production upgrade, installation or fresh-store admission. Supported launchers and restore paths must reject an actual schema-12 candidate before access or worker retirement. No additional ownership capability ID is needed unless delivery separates these changes.
- **Export tombstones the replica.** Reverse export renames the marker to `mode.json.exported-<utc>` and the replica and the live store to `collections.sqlite.exported-<utc>`, so no host can adopt them; the tombstones stay as evidence. Its order is the intent, the replica publication (which copies the store because its intent changed, though its head did not), the files and checkpoints, their verification, the `verified` record in the intent, the fence retire, the marker, the replica, the descriptors and, last, the store, which carries the intent until that final rename. After a retire, a rerun goes on from the frozen store only when the coordinator's newest retired fence, read through a separate route that released clients never call, names this store with a head in its chain: a verified intent finishes the renames, and an unverified one first writes and verifies the files. While the fence stands, the owner may abandon an export that cannot finish (P1b.4a): one store transaction records each file that holds the export's bytes as its view, records each checkpoint that the activity log holds as the next export's legacy parent (a checkpoint cut between its manifest and log writes is not in the chain, and the next export rewrites that manifest), and deletes the intent, then the store publishes and records its head. Before the intent, the export probes the fence retire and the retired-fence read, so it never retires a fence on a coordinator that cannot serve its rerun. Re-migration mints a new `store_id` and enrols the fence at the next generation.
- **Coordinator prerequisite (ruled N3).** Wherever writer coordination is configured, single-host or multi-host, preflight explicitly probes the coordinator's `collections-store-v1` store-fence route before store adoption. An old coordinator silently ignoring an extra acquire/renew field is not proof. If the capability is unavailable, migration refuses with `COLLECTION_STORE_COORDINATOR_UPGRADE_REQUIRED`, naming the exact release to upgrade the coordinator to: the running release's version, which declares `collections-store-v1`.
- **Fencing old releases.** Store adoption advances a separate vault-specific collection-store capability fence by generation CAS, invalidating the current lease token in the same coordinator transaction; the store-aware holder reacquires before marker cutover. This uses the existing fence machinery but does not advance governance versions 3/4 or enroll file-only vaults. Acquire/renew satisfy each independently applicable fence. Mixed/store vaults also record `collections-store-v1` in the state manifest's descriptor list so old runtimes refuse before access; new runtimes distinguish this optional compatibility descriptor from mandatory migratable state families. File-only manifests stay unchanged. The coordinator fence alone proves writer admission, not old-reader refusal. The documented downgrade path is **export before downgrade**, and the doctor probe says so.
- **Optional compatibility, not a migrated family.** Required physical descriptor families remain the set returned by `declared_descriptor_ids()`. A closed catalog partitions recorded descriptors into physical families and recognized optional compatibility IDs; unknown IDs never disappear through subtraction. A complete manifest has exactly the physical families, all complete, while readiness additionally requires support for every recorded optional ID. Family upgrades and external-state adoption preserve recorded optional IDs rather than inventing a family or dropping the older-reader fence. Fresh file-only bootstrap records only required physical families.
- **Compatible upgrades keep the ordinary handoff.** Candidate interpreter probes separately report required families and supported optional compatibility IDs. Required-family target declarations match the inspected interpreter; optional support remains private verified metadata bound to that exact candidate, never an extra field in managed target requests, replies or retained transition targets. Their legacy `{python, version, state_descriptors}` triple remains unchanged. Missing, mismatched or failed verification means no support. Reject an incompatible candidate before stopping the serving worker and recheck fresh admission at quiesced cutover; restore admission before rejected-candidate cleanup outside the stop/migration timeout. Compare only physical families to decide whether migration is needed. An otherwise compatible store upgrade therefore skips needless migration. Recognizing an optional ID is not a support promise: a runtime lists an ID in `state_migration.supported_state_compatibility_ids()` only when it can use that format. This runtime lists `collections-store-v1` unconditionally, because it opens and serves a store whatever the release capabilities say; those capabilities (`records-summary-v1`, `collection-views-v1`, `collections-migrate-v1`) decide only which store routes it runs and whether it moves file collections into one.
- **Enrollment belongs to the create/adoption adapter.** One internal, idempotent emitter adds only `collections-store-v1` to a complete manifest under the existing migration lock, preserves other fields and uses durable publication after rechecking the trusted authority callback. It offers neither arbitrary IDs nor removal and invalidates cached resolution. Its future caller supplies the mutation boundary, recoverable create intent and store/fence/old-process binding; enrollment precedes marker cutover and survives post-commit interruption. The helper neither migrates a family nor establishes a stop window, and has no production callers until that adapter exists.
- **Private new-collection preparation.** Reuse the canonical create transaction, audit, immutable receipt and existing pending intent. Preparation publishes no vault files and grants no production enrollment. Projection publication and orphan recovery use the same complete admitted ownership proof. Exact retry preserves the committed collection and transaction, while an unresolved binding preserves intent and conflicting bytes. The marker remains a transition preimage, not an independent membership authority.
- **Mixed marker representation.** The S1 representation retains `mode: "store"` and adds `version: 1`, `default_authority: "file"`, `authority_epoch` and `collections` entries containing `collection_id`, `manifest_path`, `authority: "store"` and the same `store_id`. Its existing `collection_store_fence` binds the capability and generation. This represents a store-authoritative entry beside file-authoritative legacy collections, not a new whole-vault mode or migration. Registry and stored marker metadata only validate consistency. The private preparation seam does not publish this marker or establish production admission.
- **Recovery preserves the current head.** The intent pins the immutable creation transaction, not a head to replay over later work. Recovery verifies that transaction in the current chain and reports the current validated head only after reconciling acquisition evidence. An already-enrolled same-store create preserves its fence, holder and later head; only initial enrollment requires the explicit authority cut and reacquisition. Clearing local intent after cutover need not erase historical bookkeeping from an unchanged shared snapshot and does not justify a new business transaction or forced publication.
- **State adoption is not collection export.** The keep-vault state remedy must refuse an enrolled optional collection-store contract before deleting external members or its compatibility fence; only the future validated collection export/downgrade protocol retires that authority. File-only adoption stays unchanged. A false refusal preserves the running service and data rather than permitting canonical loss.

**Capability fence identity transitions.** The authenticated operator CAS also permits a new store identity after export/re-migration; it does not perform that workflow. For current `(store_id, generation)`, a same-target/capability request with expected generation equal to current or current minus one is an idempotent replay, preserving head, holder and token. A different target requires the current generation and atomically advances it, clears only the previous identity's head and revokes the holder/token. Generations never reset or disappear. Replaying an acknowledged identity transition after new head reports must not clear those reports again. The future adapter quiesces/exports and retains the old identity/head in durable transition evidence before this CAS, then reacquires before marker cutover. A stale replay after intervening transitions conflicts, even if a target UUID recurs.
- **Import binds to exact bytes.** In §10 step 5, `published_sha256` is the hash of the bytes the importer actually parsed, captured in the same read, not re-read later. A file that changes between parse and publication is therefore a human edit by construction.

**Earlier dark create/restart tranche (2026-10-04).** The following sequence records the earlier marker-based implementation checkpoint. The canonical ownership rules above supersede marker membership as runtime authority. The isolated producer remains useful recovery evidence; it proves neither production enrollment nor GA acceptance.

Custody comes from one private context-managed producer session that allocates its isolated root and retains held directory identity, process-lifetime ownership and exact vault/marker/replica bindings. Arbitrary existing paths, caller booleans, permissive callbacks and serialized isolation claims cannot mint it. A surviving isolated supervisor may retain that ownership while restarting a producer child; the child reacquires the existing canonical mutation boundary and validates the durable intent. Reuse the held-filesystem and mutation-lock primitives, without a second journal or general custody framework.

1. Under the canonical consistency boundary and existing writer authority, prepare the canonical create and its immutable receipt/intent. Leave the old-token authority scope and close its preparation handle before the initial fence CAS; retain process/boundary ownership. Attach the single runtime in restricted bootstrap state so capability-bearing reacquisition is possible. Another collection in the same admitted store preserves its fence generation and holder.
2. Retain the actual acquisition record and reconcile its reported head against the current verified local chain before reporting any local head. A token-bound bootstrap state gates ordinary checkout, head reporting and release together. Lease renewal may continue with the capability and no head while reconciliation/publication is pending; a later status read is not a substitute for acquisition evidence. A false gate delays this create/recovery, not renewal or unrelated file-mode work.
3. Enroll the fixed optional descriptor under the existing manifest lock. Dark restart validates the complete physical manifest and only that exact optional descriptor through a producer-owned recovery path tied to process custody, store/intent and writer authority. It neither advertises global support nor supplies a generic compatibility bypass. A crash immediately after enrollment must resume in a fresh process without monkeypatching runtime support.
4. Resolve same-instance pending replica scratch under the new token while its stored lease epoch still matches the intent, then stamp the new epoch and publish through the current validated head. Track the installed copy's lease epoch separately from its business head. An unchanged head cannot skip publication after an epoch change; an unchanged same-instance re-admission keeps its epoch instead (A3). Preserve strict pending-record validation. Head reporting opens only after acquisition evidence is reconciled and that publication is durable. Token loss stops the operation; a separate resume may reacquire.
5. Recheck the exact token, store/fence binding, current head and marker preimage before durable marker installation. First installation is no-clobber. Replacing an existing marker additionally requires producer-established custody of the marker namespace, not merely private replica staging: the held-filesystem replacement primitive is not a content compare-and-swap against incoming sync. In this tranche that custody is actual isolated ownership, not an attestation flag. Unsupported custody leaves the create intent pending; the cost is delayed creation for that owner, preventing lost routing state. Never displace an existing marker through a temporarily absent name, which would expose file authority.
6. Admit projections by current marker membership continuously, including after intent cleanup. Resolve an exact retry through current governance, request hash and the immutable creation transaction in the current chain, verifying that its collection remains admitted. Clear the intent only with that replay/admission path in place; cleanup creates no business transaction or forced replica refresh.

Local startup, restart, explicit backup restore and fresh-root old-reader rejection through supported launchers remain mandatory before single-host S1 enablement. Isolated create/restart evidence proves none of those deployment conditions. Cross-host takeover, copied-host adoption and fleet custody acceptance are deferred beyond this GA. Existing foreign-head checks remain intact.

#### A6. The legacy importer has no ceiling

Today `_audit_events` returns empty past its source-byte, event and archive bounds (8 MB, 10k events, 128 archive entries; `records.py:2437-2495`, `:1945`), and `_reconstruct_audit_chain` stops at depth 2048 (`:2124`). An importer built on them would silently lose history.
- **An uncapped streaming reader for the importer only.** It reads `log.md` and every archive segment in order, filters by `collection_id`, and verifies the chain incrementally in constant memory per collection. The bounded readers stay for file-mode inspection.
- **The P1b.1 test vaults include** a vault whose logs exceed 8 MB across more than 128 archives, and a collection chain deeper than 2048.
- **A verified-through watermark.** `collections.verified_through_txn` records a validated collection prefix. Admission validates the prefix before trusting that value. Inspection verifies the new suffix under its read snapshot and never writes a watermark. An admitted writer advances it within its existing transaction, after checking the old prefix and new event material. The final `txns` insertion remains last. Rollback preserves the previous verified point.
- **History pages split by entry count** (500 transitions per page, `_history/0001.md`, …), not by year. Ordinary append pagination rewrites only the main page and newest numbered page. Governance invalidation and repair of edited generated pages may regenerate older pages. No page grows without bound.

**Canonical audit reader.** One `collection_store/history.py` reconstructs native events from immutable transactions, manifest versions, item effects, sources and receipts. Imported events retain their original immutable envelopes. The existing file audit walker cannot supply canonical history because it requires activity files, markers and file-mode bounds. Reuse its public envelopes and validators instead of adding another history format. Every native event hash input must remain reconstructible; transient proposal bytes belong in request identity. Verify control events without treating them as missing business transitions, since they do not advance the collection's business head.

The existing writer handle may retain bounded scalar prefix proofs under its canonical identity and invalidation boundary. Unexplained SQL changes, rollback, schema changes and handle replacement invalidate that trust. An invalid derived watermark discards prefix trust and requires verification; it does not establish canonical corruption. Public inspection and query verify under current snapshots without maintenance or publication. Corrupt canonical history raises an integrity error; it never becomes an invented native gap. Imported assessments remain provenance facts; current governance may further report `history_incomplete`. Exhaustive verification does not upgrade an imported assessment or make unknown history acknowledgeable.

**History projection.** Reuse `projection_state(kind='history')`, the existing publication batch and the asynchronous drain. A mutation records pending history work in its own transaction; it never renders history on acknowledgement. Factor the existing equivalence-domain machinery in `governance/projections.py` into a shared helper, including unnamed/default audiences, purposes and synthetic session-grant states. Reuse the current decision kernel. Recheck canonical page contents at egress; a stale page cannot release effects that current governance withholds. Page numbers, stamps, public versions, content hashes, continuations and navigation depend only on released history. Omit metadata fields that would reveal hidden-only changes through a raw generation or audit head.

History selection and queued publication have separate progress. Inspection may advance selection without queuing a page. A bounded active sweep freezes its target, reader counts and numeric continuation. Commit cursor advances with existing pending intents; rollback discards them. Appends cannot reset the sweep, and incomplete discovery keeps the existing main-page pending signal awake. Handle loss restarts discovery without discarding committed intents. Charge this state to the existing cache budget; add no durable index or second authorizer.

Native and imported row history reuse `record_history` envelopes, origin projection, actor redaction and typed version decoding. Merge authorized retained legacy sidecar entries with native versions, since import stores only the current payload as version 1. Report `unavailable_legacy` only for genuinely missing retained content. Store rebaseline permits only an imported positive gap and uses existing lifecycle fingerprints, guards and receipt validation. Its checkpoint preserves the permanent discontinuity after later writes and restart. Native collections and unknown imported history refuse rebaseline. The activity log remains unchanged.

#### A7. Budget holds for non-uniform collections; no second fsync

Earlier, §12 assumed uniformly released collections. Two changes fix that:
- **Release-decision cache.** The existing writer connection owns bounded canonical metadata, pure scope membership and base-policy results. `(audience, policy fingerprint, collection_id)` is a namespace, not the whole validity key. Reuse additionally binds resolved purpose, manifest/type/default-audience/path/layout identity, current item `(row_id, row_version)` or held governance identity, freshly loaded access fingerprint/blocked state, exact tombstones, and read/mutation mode. Manifest authority is checked separately from row-release summaries. While this complete dependency tuple is unchanged, a committed row change re-evaluates only changed subjects and adjusts the complete-release summary. Policy, purpose, access, lifecycle or contract changes may require a broader identity-only pass; cold fill and eviction may do the same.
- **Fresh operation authority.** Each operation still verifies the complete principal and exclusive owner thread (A3) before SQL, and refreshes session status, credential generation, audience, purpose, clock, active grants and authority failure state. Final grant-bearing decisions are never reused across operations. The existing authority-validation core resolves grant-named canonical identities lazily; its catalog entry point remains a compatibility wrapper, and issue, redeem and consume share the current canonical basis. No second evaluator or manual ceiling composition is introduced.
- **Committed cache deltas.** Proposed subjects remain transaction-local until COMMIT succeeds; operation close is not that boundary. Item and held changes report their exact committed identities, manifest/type changes invalidate the subject epoch, and unexpected connection changes invalidate reuse. A rollback must not leave a cached proposed row version that can be reused for different content. Closing or replacing the store handle discards its cache. Internal cache epochs and hidden-only collection changes never become partial-release continuation identity.
- **No second transaction.** `projection_state.pending_sha256` (the exact rendered bytes' hash), `pending_row_version` and installation context are written inside the main transaction. Item and manifest staging and installation run afterward through the background projector. After a successful install the pair is promoted to `published_*` lazily, by the next transaction that touches the view or by the reconcile. There is no extra commit or fsync on the acknowledgement path. A1 step 4 accepts either `published_sha256` or `pending_sha256` as expected on disk.
- **The P1a.15 gate adds** a non-uniform 10,000-row collection (a ref-scoped rule withholding 10% of rows) and a Windows/NTFS run. A partially authorized audience must refuse a write; measure that refusal and successful fully authorized appends separately. Full public `inspect`, not a reduced internal guard lookup, remains the guard-refresh acceptance path.

#### A8. Withheld-as-absent: the remaining leak surfaces

- **The replica and markers are reserved.** `Knowledge Base/_Collections/` (the replica, `mode.json`, staging, aside, `.foreign-*` and `.exported-*` copies, and sync-conflict copies of any of them) is reserved in `reserved_paths`. It is denied on every egress path: `get_page`, `find`, `list_directory`, remote command surfaces, the lexical, embedding and graph indexes, and the resolver.
- **History pages are rendered for the intersection of audiences** that can read the page's path. An effect appears only if every such audience can read its item. A mixed bulk transaction with any withheld effect is rendered without its `why`.
- **Filename-recipe view paths** can carry titles. Each surface below authorizes the item behind a view path before exposing the path, and each has a test in P2.4:
  - `list_directory` and directory counts;
  - `list_inbound_links` and graph neighbours or backlinks;
  - the wikilink resolver and title lookup;
  - lexical and memory-refs indexes of views;
  - `get_page` refusal shapes (withheld equals missing);
  - `move_file` / `delete_file` refusal messages;
  - attention-queue and due-state entries;
  - history-page links;
  - held-correction views: a hold is released no further than the existing item it would change, whatever selector withholds that item;
  - activation item anchors (for types that surface);
  - inventory counts;
  - sync-conflict copies of views.

#### A9. Backup: the live store is never copied as a file

"Exclude `-wal`/`-shm`" (§11) was wrong for a live WAL database: copying the main file alone is also inconsistent.
- The live store is **excluded entirely** from file-level backups.
- The supported deployment has no restic timer. An external backup takes `exomem collections backup --to <staging>` or `--stdout` for a consistent snapshot, alongside the vault replica.
- The P1b.8 gate measures **replica sync churn**. A full replica is about 15.8 MB at 10,000 rows (spike), so publication is coalesced to at most one publish per 60 s under steady writes (and always at the §1 boundaries). The gate reports bytes per hour at 1, 10 and 60 writes per minute.

**Measured churn, 2026-10-18 and 2026-10-19.** Four 300-second windows ran on one retained invented fixture of 10,000 Work rows, with a real in-process coordinator and released item, manifest and aggregate views. Three ran at source `1bf69cb51`; the 60-writes-per-minute rerun ran at `99f136bd8`, which includes #1892. Installed bytes count replica files replaced inside the window, not physical I/O or network. Publication attempts land about 60–65 s apart. Three windows had one busy attempt: it returned `retry_pending` (`COLLECTION_STORE_BUSY`) in about 1 ms, with no pending publication and an unchanged replica signature, so these rates count it as installing nothing, and its replacement waits for the next attempt. The probe marks those windows partial.

| Writes per minute | Source | Probe status | Writes acknowledged | Replacements | Installed bytes | Bytes per hour | Acknowledgement median / max, s | View appearance after acknowledgement, s |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `1bf69cb51` | partial (one busy attempt) | 5 / 5 | 4 | 106,729,472 | 1,280,752,776 | 0.087 / 0.417 | 0.86–1.06 |
| 10 | `1bf69cb51` | complete | 50 / 50 | 5 | 133,619,712 | 1,603,436,080 | 0.086 / 0.420 | 0.86–0.99 |
| 60 | `1bf69cb51` | partial (one busy attempt, two missed slots) | 298 / 300 | 3 | 81,375,232 | 975,360,424 | 0.982 / 1.508 | 0.55 to an upper bound of 27.55 |
| 60 | `99f136bd8` | partial (one busy attempt, five missed slots) | 295 / 300 | 3 | 83,099,648 | 995,418,590 | 1.005 / 1.626 | 0.21–5.08 |

Inside each window, replacements land at least 60 s apart, so the coalescing budget holds; a busy attempt stretches one gap to about 120 s. Closer publications occur only at service start and stop, which are §1 boundaries. A replacement installs the whole replica, about 27.5 MB at this size. Steady writes therefore cost one replica per publish interval, about 1.6 GB per hour at every measured rate. The lower rows hold fewer replacements for two reasons. At 1 write per minute a busy attempt fell inside the 300-second window. At 60 writes per minute a busy attempt also fell inside it, and the first replica attempt came at about +62 s instead of +2.7 s, so only four attempts fell inside it. The lower rows do not mean that a higher write rate costs less. At 60 writes per minute the acknowledgement median reaches about 1 s, and the probe misses two and five scheduled slots; that saturation is not attributed. The title-only manifest revision on this fixture took 24.12 s at `1bf69cb51` and 1.63 s at `99f136bd8`, one sample each; #1892 is the change between them on that path. Its manifest view appeared 1.87 s after acknowledgement. The rerun's report SHA-256 prefix is `4b3b75ee457b9215` and its probe prefix is `a88cdf1086fa3ec5`.

#### A10. Moves and renames: `VIEW_MOVED`

A moved view keeps its item identity through its stamp, and Obsidian rewrites links to the new path, so re-rendering at the old path would break those links.
- **A move within the collection's source root** with an intact current stamp is a governed `view_move` transaction. It updates `items.view_path`, is audited, is re-authorized because the governance path changed, and the view is not re-rendered at the old path. The next filename-recipe render keeps the moved path until an explicit representation maintenance runs, matching today's `filename_drift`.
- **A move out of the root or into another collection** is held as `VIEW_MOVED`. The item keeps its canonical path, which is re-rendered, and the moved file is held with its bytes.

#### A11. Scope, stdio installs and the cutover budget

- **Split into a follow-up change**: pinned version links (§14.3) and the compiler-lane rewrite (§14.4, formerly P4.5). The worked example's pinned executions and surfacing turns are delivered there. In this change, Recipe Executions uses an unpinned link, and types surface only as today's Records and Planning do.
- **Non-managed installs** (stdio MCP, no service manager) migrate with `exomem collections migrate`. It is offline, takes the mutation lock, refuses if a service holds the vault, and runs the same preflight, import, proof, mode marker and descriptor steps.
- **Store mode requires lease coordination** (D7, ruled by the operator). The capability fence, the head exchange and takeover live in the coordinator, so an install without one cannot protect a store. It runs the single-host coordinator that `docs/deployment.md` describes. Without one, the preflight reports the vault `blocked`, and every store step (prepare, apply, enrolment, a summary create, serving a store and adopt-local) refuses with `COLLECTION_STORE_COORDINATION_REQUIRED` and names that setup. Before D7, these steps refused with `COLLECTION_STORE_LEASE_REQUIRED` or `COLLECTION_STORE_CUSTODY_REQUIRED`, and the enrolment refusal named a command that also cannot run without a coordinator.
- **Handoff within the supervisor's 15 s migration budget** (`service_manager.py`). The migrator stops itself 9 s after its spawn, leaving 6 s for the steps from the seal on. The proofs of collections whose basis is unchanged since pre-import are carried forward. Only changed collections are re-imported and re-proved at handoff. If that delta is not proved in time, the upgrade is abandoned: the migrator publishes nothing and exits 0, the release completes in file mode, and the migration retries at the next handoff. Raise the cap only if measurement shows that the audit scan alone needs more, and prefer an incremental scan. Step 0 (2026-10-16, unverified: invented data on one shared host, measured with a scratch timing script outside the repository): 2,400 Records items in four collections, with 4,804 legacy events and 5.5 MB of activity logs. A cold first run took 0.8 s to scan, 4.1 s to capture and 3.4 s to import and prove, at 246 MB peak RSS. A warm rerun took 0.2 s, 1.7 s and 0.8 s, at 131 MB; its carry-forward compare (scan and capture, no import) took 1.4 s.
- **A vault with a live S1 store** migrates by copy-then-swap. Staging is a backup-API copy of the live store plus the imports. Cutover requires the live store to be unchanged since the copy; otherwise the managed path abandons and the offline path copies again.
- **Until the marker is installed, the files stay canonical.** The migration intent keeps every collection's basis, and a resumed cutover compares the files with it before the fence cut or the marker. The mutation boundary is shared only by processes with the same lease state directory, so every apply repeats its comparison under the writer lease before it publishes, cuts the fence or installs the marker. While the fence is unenrolled, nothing fences or routes the published store, so a changed collection supersedes it with fresh staging. Once a fence binds it, writers inside the lease are fenced from those files, so a change can only come from outside the lease. Admitting would lose it, so the apply supersedes the unadmitted store instead (P1b.3c). On a first enrolment, no marker routes to that store: fresh staging imports the change, its intent records `supersedes` (the store and generation the fence binds) and targets the next generation, and `_store_fence` moves the fence to the new store by one CAS, which clears the recorded head and revokes the token. The new store's first publication keeps the superseded store's replica as `.foreign-*` evidence. On an S1 vault the marker routes the store that the migration superseded, so the apply restores it with `replace_live`, after it proves that the migrated store is that store plus only this intent's imports: the restored store's head equals the intent's `base_head`, and every later transaction imports one of the intent's collections. When the migrated store's publication or head reached the vault replica or the coordinator, the restored store continues as a new tenure through adopt-local's apply body, then publishes and records its head, and the route migrates again under a fresh lease acquisition. A rerun recognises a restore that stopped before its fork by its evidence: the newest superseded store carries a migration intent for the current marker, and the live store still ends at that intent's `base_head`. Until that fork, takeover refuses the migrated store's replica, so the restored store commits nothing; a restored store that committed since was admitted, and the rerun migrates it as any live S1 store. A failed proof refuses with `COLLECTION_STORE_MIGRATION_STALE` and changes nothing. Only the owner's apply finishes a migration; a serving store never resumes one.
- **Changed-only handoff refresh.** Separate legacy materialization from its SQL assembler. Fresh imports and retained imports use that assembler, which allocates destination identifiers and derives the global chain. Preserve the live-store chain prefix and exact retained envelopes, source bindings and proof identity. Never delete immutable rows or disable triggers to replace a collection.
- **Retained proof eligibility.** Reuse only an unchanged, never-written JSON-v1 import with initial row and manifest versions, original projections and import-only history. Basis version 2 records a fixed materializer/proof-format version and a digest of the shared normalized material after successful proof. Recompute that digest when loading retained material. Old or incompatible staging requires a fresh import once. This detects material alteration and format skew; it does not authenticate an actor who can rewrite both data and evidence.
- **Private replacement custody.** Use the existing `collection-migration` identity domain and held filesystem anchors for the state directory, donor and replacement. Prepare holds no vault boundary or writer lease. Cutover takes the vault boundary before migration coordination, then writer authority. Keep pending-intent resume and restore outside the migration scope. Release migration coordination before admission or replica publication acquires another domain. Cleanup removes only known entries with their held identities and preserves unknown or changed bytes.
- **Candidate selection.** Keep the donor until a complete replacement has passed chain and foreign-key validation and all SQLite handles and journals are closed. Replace private staging atomically within its owned directory. After refresh, compare every collection basis and the live base again under the lease. Preserve unusable donors and build fresh staging instead of reopening them for retention.
- **Seal and installation.** Before final proof, require DELETE journaling and no companion files. Retain the current `FileVersion` through that proof. Inside seal's IMMEDIATE transaction, compare identity, `FileVersion` and exact basis text before any writes. Hold SQLite's [connection-local exclusive lock](https://www.sqlite.org/pragma.html#pragma_locking_mode) through commit and capture the sealed `FileVersion` before closing. After close, require that same version and no companions. Repeat those checks before installation; never adopt an unexpected version.
- **Deadline and remaining cost.** Apply the caller deadline to existing identity-lock acquisition, bounded material and envelope iteration, assembly and SQLite validation. Expiry before seal abandons publication; after seal the existing installation and recovery sequence owns completion. Changed-only import and proof do not promise constant total I/O: audit capture, base copying and final single-file assembly still grow. Measure them against the existing budget.
- **Live base rules.** A moved live S1 head abandons managed handoff. Changed bookkeeping under the same head requires a fresh snapshot, while eligible file imports can retain their proofs. The store swap leaves the derived database beside it under the same store identity.

## Risks / Trade-offs

- **Opaque storage.** Canonical rows are no longer greppable Markdown. *Mitigation:* views at today's paths, the replica as one portable file, the reverse exporter, and the history page.
- **Views can lag.** A reader of the vault sees a view up to the projector's drain time behind the store. *Mitigation:* the projector is post-commit and immediate (sub-second in the spike), inspect reports pending views, and edit-back refuses to apply an edit made against a stale view (`VIEW_CONFLICT`).
- **Edit-back surprises.** An edit can land as a held correction instead of applying. *Mitigation:* held views are visible under `Held/`, in inspect and in attention. The settle windows avoid holding mid-typing states.
- **SQLite on network filesystems** (SMB/NFS) is unsafe with WAL. *Mitigation:* the live store sits on the state root, which is a local path by default. Readiness verifies `journal_mode=wal` took effect and refuses a network-mounted state root.
- **SQLite version floor** (≥ 3.38 for STRICT and JSON). Python ≥ 3.11 builds bundle newer SQLite on Windows and macOS. Linux distributions vary. Readiness refuses with a clear remediation.
- **Two writers without the lease** can no longer merge collections through file sync. Divergence fails closed and is reconciled into held corrections (§15, item 5).
- **Multi-host RPO** equals the replica coalescing window. That matches today's file replication lag, and divergence fails closed (§1).
- **Two modes during migration.** They add temporary complexity. *Mitigation:* the switch is per vault, and deletion is a scheduled task with its own acceptance.

## Rulings

**O1 ruled:** a view moved out of its root stays where the owner put it and follows ordinary file permissions. A held copy retains canonical row governance, and the original view is restored. This creates no inherited collection authority at the destination.

**O2 ruled:** invalid item and manifest views keep the edited bytes in place until a valid edit or explicit discard resolves the correction. SQLite continues to serve the last valid record. Direct file reads withhold the invalid view because its bytes are not a valid render. Large summary collections keep rows in SQLite and publish bounded read-only pages.

The orchestrator's rulings on #1459 are folded into the sections cited. The critic's amendments 1–11 are ruled and folded into §16 (A1–A11) and the sections it names. The four follow-up points were confirmed as implemented:

- **N1 confirmed.** Views are stamped with `store_id` plus `instance_id`, and each store keeps an instance lineage `(instance_id, adopted_from, adopted_at_commit_seq, head_hash)`. Only instances outside the lineage are foreign (§16 A2, A4).
- **N2 confirmed.** Install is no-clobber: `link` then unlink staging on POSIX, `MoveFileEx` without `REPLACE_EXISTING` on Windows, and `RENAME_NOREPLACE` where available. A file appearing between the aside rename and the install is held like a changed aside (§16 A1).
- **N3 confirmed.** A multi-host vault cannot migrate until its coordinator explicitly advertises `collections-store-v1`. This is acceptable because the coordinator is Exomem's own code and ships in the same release. The preflight refusal names the exact release to upgrade the coordinator to: the version of the running release that declares `collections-store-v1`, read from that release's own upgrade manifest at runtime rather than hard-coded (§8 C20, §16 A5).
- **N4 confirmed.** The replica is published at most once per 60 s under steady writes (configurable), and synchronously at every orderly boundary: lease release, shutdown, quiesce, upgrade handoff and export (§16 A9).
- **Amendment 11** (pinned links and the compiler-lane rewrite move to a follow-up change) is noted by the owner.
- **D7 ruled by the owner (2026-10-16): store mode requires writer-lease coordination.** A stdio install runs the single-host coordinator to use the store; without one every store step refuses with `COLLECTION_STORE_COORDINATION_REQUIRED` (A11). A Cloud cell renders no lease URL today, so it serves no store; no task yet gives cells writer-lease coordination.

### Earlier rulings

- **R1 accepted.** The live store is under the per-vault state root (`external-canonical`), plus the integrity-checked single-file replica at `Knowledge Base/_Collections/collections.sqlite` (§1, §11).
- **R2 amended.** #1457 lands now as the interim. #1452 ships now on files with its enforced per-call row cap (lease held for about 5 s at most). The store adopts #1452's API unchanged, drops `BULK_UPSERT_AUDIT_DEPTH`, and raises the cap to 500 (§9 "Sequencing", §8 C12).
- **R3 accepted.** Row-level audience is expressed through existing `_Governance` scopes evaluated per row, with no per-row audience column (§7).
- **R4 accepted.** Collection mutations write no `log.md` lines. There are per-collection history pages and no vault-wide digest for now (§5, §8 C9).
- **R5 accepted.** Re-render normalizes frontmatter formatting; values, body and the log frame are exact (§5).
- **R6 accepted.** Datasets stay file-canonical and query-only.
- **R7 accepted.** File mode and the reverse exporter stay for two minor releases after store GA, then the §13 deletion runs (tasks phase P6).
- **R8 accepted.** `record_memory` serves declared types. No new tool, and the added surface is minimal and byte-budgeted (§14.7).
- **R9 accepted.** The kinds are `observed`, `intended`, `procedural` and `reference`, growing by shipped revision only (§14.2).
- **R10 accepted, with a guard.** Each type gets one top-level segment. A segment already holding non-collection files refuses with `COLLECTION_TYPE_PLACEMENT_OCCUPIED`, naming the folder (§14.1).
- **Two writers without the lease are explicitly unsupported** for collection writes. They fail closed with `COLLECTION_STORE_DIVERGED` and reconcile into held corrections (§15 item 5; the `structured-collections` snapshot requirement).
