## Context

Exomem currently has strong lower-level pieces but no collection product model. `query_data.py` reads and reduces CSV/TSV/JSON; `memory_refs.py` gives stable page identity; semantic units demonstrate anchored item identity and fingerprint-guarded mutation; `vault.py` provides guarded staged publication; `writer_lease.py` serializes same-vault mutations and supplies idempotent terminal responses; governance gates files/pages; and watchers/reconcile rebuild derived search, reference, semantic-unit, and graph state. None of those defines a collection manifest, row/block identity, collection schema, keyed mutation, per-item history, multi-representation adapter, or direct-edit reconciliation contract.

The durable Planning work defines intended future state and explicitly leaves accepted software contracts to OpenSpec/repository truth. Records is the complementary observed-state layer. There is no committed Planning implementation to extend and no hidden Records branch. A separate `add-first-class-records` change is therefore the clean delivery boundary, but its mechanics must be profile-neutral so a later Planning change consumes the same collection substrate.

The current X3 vault material is a newest-first `type: tracker` Markdown log with dated Push/Pull blocks, pipe-delimited movement rows, a separate undated archive, and ordinary Push/Pull templates. It must remain canonical and directly editable. The live `.obsidian/templates.json` discovered during reconciliation currently points to `Knowledge Base/Imported/Templates`, not the intended `Knowledge Base/Templates`; Exomem will not mutate `.obsidian`, and real-vault template-menu acceptance remains a configuration follow-up outside this repository change.

## Goals / Non-Goals

**Goals:**

- Introduce one human-owned structured-collection contract reusable by Records and future Planning.
- Make Records first-class observed state with collection-scoped identities, minimal typed schemas, safe Markdown append/update, bounded query/reduction, reasons/audit receipts, provenance-bearing output, and immediate visibility of manual edits.
- Support chronological Markdown logs, Markdown item files, and CSV/TSV/JSON without making any one shape universal.
- Preserve `type: tracker`, the X3 log/archive/templates, and immediate manual/Obsidian editing without a forced migration.
- Prove domain neutrality with vehicle maintenance and prove Planning integration with a reference/query contract, not a partial Planning product.
- Apply governance before reductions and keep high-volume raw items out of ordinary semantic recall by default.
- Expose one natural Records command through the existing generated MCP/REST/CLI registry.

**Non-Goals:**

- A complete Planning system, calendar, task executor, dashboard, board, spreadsheet, relational database, analytics suite, charting engine, form builder, TUI, Studio area, or Obsidian plugin.
- Row-level governance inside one canonical log/dataset, arbitrary joins, formulas, recurrence engines, or automatic medical/performance interpretation.
- A persistent required collection database or an automatic migration/promotion between canonical representations.
- Dataset mutation, canonical representation migration, planned-versus-recorded comparison, or a new item-level history database.
- Retrofitting explicit IDs into every historical tracker block during installation.

## Decisions

### 1. One neutral manifest, separate semantic profiles

An explicit collection is described by an ordinary Markdown manifest, conventionally adjacent as `_collection.md`, with `type: collection`, the existing `exomem_id` UUID field, `semantic_profile`, collection/schema versions, title/domain/lifecycle, `storage`, `item_schema`, and optional `templates`, `views`, `governance`, and `links`. The body is human-facing context. Records conventionally live under `Knowledge Base/Records/`; a later Planning product can use `Knowledge Base/Planning/` while loading the same contract.

`semantic_profile: records` activates observed-state constraints. `semantic_profile: planning` is parseable by the substrate but its domain operations remain unsupported until the Planning change. This prevents “universal database” semantics while avoiding two mechanical implementations.

Compatibility discovery also recognizes an existing `type: record-index` or `type: tracker` as a legacy collection surface. Without an explicit manifest it receives a clearly marked path-derived compatibility identity for collection-level discovery and inspection only. Item parsing, query, append, or update requires an adjacent manifest—or an equivalent explicit caller-supplied descriptor—with the complete bounded adapter grammar. This keeps the core generic: it never guesses whether a heading, legend, template fragment, or delimited line is a Record. Adding the manifest is the explicit route to move-stable collection identity and item operations, and it does not rewrite the tracker.

Alternatives rejected:

- A SQLite collection catalog: easier lookup, but it would hide the contract and make recovery/manual use depend on derived state.
- Separate Records and Planning manifests: clearer naming at first, but guarantees schema/mutation/query drift.
- Templates as manifests: couples entry convenience to binding schema and makes historical behavior implicit.

### 2. Adapter protocol over three canonical shapes

`structured_collections.py` owns manifest/schema/identity/query-neutral types. `record_formats.py` owns an adapter protocol and three adapters:

- `markdown-log`: a declared section and fence-aware heading/block grammar plus optional delimited child rows. Child rows declare their destination `container_field`, which must be an array-of-objects field in the item schema. It parses current blocks without rewriting them, inserts at the declared newest/oldest edge, and returns exact source spans.
- `markdown-items`: one file per item, with `type: record`, `record_id`, `collection_id`, schema version, domain fields, and optional body.
- `dataset`: query-only CSV/TSV/JSON using shared `query_data` loading/coercion/evaluation. A declared stable-key column improves references and deterministic ordering, but append/update is deferred until a format-specific preservation contract exists.

The Markdown-log grammar is declarative (heading level/fields, section, insertion direction, child delimiter/fields/container), so the generic core does not contain X3 conditionals. X3 fixture configuration maps dated headings and `movement | band | reps` rows. Raw notation such as `+`, blanks, `!`, or `?` remains data rather than being normalized into a judgment. Markdown-item reads accept one leading UTF-8 BOM for frontmatter parsing while preserving the complete original bytes for source hashes, spans, body behavior, and any later guarded rewrite.

The X3 fixture supplies an adjacent manifest whose descriptor maps dated headings and `movement | band | reps` rows while leaving `Training Log.md` byte-identical before the first requested mutation. A third fixture proves bounded dataset querying without claiming byte-preserving dataset mutation.

### 3. Collection-scoped identity, exact source versions, and replay hashes

Collections reuse `memory_refs.new_id()`/UUID validation. An item identity is universally the tuple `(collection_uuid, canonical_item_key)`. New mutable Markdown items use a UUID key stored as `record_id`; a query-only dataset may use a manifest-declared bounded string key. Arbitrary dataset keys are not called `exomem_id` and do not collide across collections. The durable standalone form is `exomem://record/<collection-uuid>/<percent-encoded-key>`.

New agent-authored log blocks include an unobtrusive ordinary Markdown HTML marker immediately after the heading:

```markdown
<!-- exomem-record-id: 01234567-89ab-4cde-8012-3456789abcde -->
```

The marker is human-owned, survives moves/reordering, and does not alter rendered Obsidian content. Manually inserted legacy blocks without markers get an inferred compatibility key only when the manifest-declared natural key is unique. Canonical natural-key serialization is canonical JSON over `[schema_version, [[field, value], ...]]` in declared field order: strings are Unicode NFC, dates/datetimes are schema-normalized ISO 8601, null is JSON `null`, and numbers/booleans keep their JSON scalar types. The compatibility key is UUIDv5 over that serialization and the collection UUID. It may change when a natural-key value is corrected, so it is explicitly not a durable substitute for an explicit marker. Duplicate natural keys remain readable with ambiguity metadata but cannot be targeted until the user disambiguates them. New IDs never depend only on date, because legitimate same-date events exist.

`item_version` is SHA-256 over the exact source bytes for the Markdown block or complete item file. `collection_snapshot` covers the manifest and ordered canonical-source byte hashes. A separate canonical-payload hash supports append replay equality; it is never used as the stale-write token. Query-only datasets expose the source snapshot and a declared key where available, but no mutable item version. These hashes are concurrency tokens, not confidence or authority scores.

### 4. Minimal schema and no meaningless universal fields

The collection schema supports required/optional fields, primitive/date/datetime/enum/array/object types, units metadata, link fields, and a declared natural key for legacy resolution. Only collection ID, item ID, and schema version are substrate mechanics. Occurred/recorded time, status, provenance/capture, uncertainty, reconstruction, relations, units, and lifecycle appear only in domain schemas that use them.

Inference samples bounded current items and returns an advisory proposal. It never rewrites a manifest or history. Schema versions are strict; unknown future versions refuse mutation.

### 5. One thin `record_memory` front door

Add a single generated product command with five top-level actions:

- read-only: `inspect`, `query`;
- mutating: `create`, `append`, `update`.

`create` create-only publishes a manifest and, when requested, an empty Markdown log or item directory scaffold; it never adopts or rewrites an existing tracker implicitly. `inspect` resolves one collection and returns its contract plus report-only schema/identity/template issues. `query` covers list/history/render/export use through bounded filters, `include_agent_history`, a saved-view selector, and `output_format` (`json`, `markdown`, or derived `csv`); it never writes an export. `append` and `update` accept structured items/changes, a canonical item key, expected container hash/item version, and a required reason. Per-action required and forbidden arguments are validated explicitly. Storage strategy is resolved from the manifest and never appears as a family of public tools.

MCP annotations remain command-level, so the mixed command is advertised conservatively as write-capable even though the selector registry routes `inspect` and `query` without writer authority. Unknown actions fail closed.

This new command is justified despite registry blast radius: existing `remember` creates compiled conclusions, `observe_memory` mutates semantic units inside compiled pages, `manage_memory_file` is a Tier-2 raw file escape hatch, and `query_dataset` is read-only/tabular. None can safely express keyed collection append/update/query without leaking storage mechanics or collapsing Records into Notes.

### 6. Bounded guarded mutations reuse the existing writer boundary

Mutation flow:

1. Resolve manifest/adapter and validate the requested structured payload outside the critical section where safe.
2. Enter the existing command invocation/writer boundary.
3. Re-read the manifest and canonical source with `read_guarded_text`; re-resolve exact ID/insertion target.
4. Validate schema, duplicate identity, expected container hash, and expected item version.
5. Render only the new/changed item span while preserving every untouched byte and newline style.
6. Stage the canonical source plus a defined agent-mutation audit entry through `plan_log_writes` and `batch_atomic_write` guarded publication.
7. Return the common terminal mutation response plus collection/item before/after hashes and affected paths.

Exact replay with one item key and canonical payload hash is idempotent; key reuse with different content is `RECORD_ID_CONFLICT`. Missing/duplicate targets refuse; there is no fuzzy fallback. Guard failures are `STALE_RECORD`. A required `why` lands in the existing human-readable activity history with `operation`, `collection_id`, `item_key`, `before_item_hash`, `after_item_hash`, `before_container_hash`, `after_container_hash`, and rationale. New agent-authored canonical blocks/items also carry one visible content-free mutation correlation marker, paired with the activity entry; this lets report-only inspection distinguish unmarked pre-existing human material (baseline) from even a first canonical-before-log interruption. This is agent-mutation audit history only: direct edits produce detectable snapshot/history gaps rather than invented events. Governance receipts, operational journal, activity history, and terminal receipts remain separate rather than creating a fourth authoritative ledger.

The terminal outcome becomes committed only after every planned replacement completes. Existing batch publication provides per-file atomic replacement and caught-error rollback, not cross-file crash atomicity. `inspect` reports a canonical/audit mismatch after an abrupt partial publication; canonical Records remain truth and repair is explicit. Successful normal execution produces the matching audit entry, while interruption can leave a visible history gap rather than a false claim that no mutation occurred.

Internal portable publication replaces the containing file atomically; “bounded mutation” means exact logical targeting, limited caller payload, byte preservation outside the span, validation/CAS, and no silent whole-document semantic rewrite—not an impossible in-place filesystem transaction claim.

### 7. Live canonical query with bounded snapshot pagination

Refactor the pure filter/sort/aggregate portions of `query_data.py` so adapters can feed rows without copying query semantics. Query supports field/relation/date filters, projection, deterministic sorting, positive hard-capped pagination, bounded aggregates/distinct/grouping, and a source snapshot token. `limit=0` no longer means unbounded. A continuation token binds to collection/query/snapshot and refuses when a direct edit changed the sources.

Markdown-log adapters can return item rows and expanded child rows; X3 movement rows retain the parent session key/version. `query` can render bounded Markdown or CSV alongside structured provenance: collection ID, exact normalized query/view definition, source hashes, generation time, and `derived: true`. It returns content but does not write or promote it in this delivery.

Queries parse current canonical files. There is no required collection index, so a valid direct editor/Obsidian change appears on the next fresh query. File/item/response caps make the scan-based first delivery honest; a future derived index can implement the same interface.

### 8. Report-only inspection; maintenance owns repair

`record_memory(action="inspect")` reparses canonical files and reports source hashes, schema violations, missing/duplicate IDs, ambiguous legacy keys, missing templates, audit-history gaps, and stale saved-view provenance. It never adds IDs, fixes values, rewrites templates, or promotes a representation automatically. Existing watcher and `maintain_memory(mode="reconcile", dry_run=false)` remain responsible for generic derived lexical/vector/graph/reference repair. Dataset-file watching beyond live query freshness is deferred unless a small extension is necessary for stale search-row cleanup.

### 9. Governance runs before reductions at representation granularity

Manifest discovery is bounded, symlink-safe, and limited to governed vault-relative roots. A path/ref resolves directly. UUID lookup enumerates candidate manifest paths without parsing identity-bearing contents, authorizes each candidate path, then parses only releasable candidates; duplicate detection occurs only among those candidates, so withheld paths remain indistinguishable from absence. Any manifest index is derived lookup acceleration, never authorization truth. Canonical sources and templates must resolve to governed vault-relative paths and are authorized before parsing or return.

For file-per-item storage, each item path is authorized before filter, total, sort, pagination, group, aggregate, latest, profile, rendered view, or export-shaped output. For a log/dataset, the canonical file is the first-delivery governance boundary; every contained row shares it. Mixed-sensitivity rows require separately governed files/collections until row policy is deliberately designed.

Records egress registers all path/ID/relation/history/conflict/count fields. Withheld collections use the existing indistinguishable-from-missing shape. Stale/conflict errors expose no current values. This reuses the release plane rather than post-filtering an aggregate that already leaked.

### 10. Collection-level recall, structured-only raw items

Manifests stay fully semantically discoverable. One centralized corpus/path predicate treats raw items under `Knowledge Base/Records/**` as structured-only by location, including Markdown item files and canonical logs, while allowing `_collection.md` and explicitly marked bounded summaries into ordinary recall. It is reused by BM25/FTS, vector, graph candidate, filter-only, auto-widen, incremental, move/delete, and reconcile paths while retaining collection-aware stable-reference and structured-query access. Raw datasets already remain outside embeddings. A manifest-less legacy tracker remains available to explicit Records discovery/inspection even when it is not an ordinary semantic candidate.

No model is added. Parsing, validation, query, aggregation, and view rendering are deterministic measurement/transduction within the pure-substrate boundary.

### 11. Templates and packs are suggestions, never hidden behavior

Collection manifests reference ordinary template paths and default properties; schemas remain independent. Templates can be inserted manually in Obsidian or any editor. Template changes never rewrite history. Existing health/personal-records pack guidance and bootstrap teach Records use through their current validated extension surface; machine-readable collection blueprints and activation are deferred. Selecting a pack creates or migrates nothing silently. All shipped scaffold/pack examples remain generic.

### 12. Planning integration is a reference/query contract only

Manifest `links.plans` can carry an opaque Planning reference plus a bounded Records query descriptor. Records validates, stores, returns, and governance-projects that descriptor but does not resolve the Planning object, compare intent with observations, infer progress/completion, or mutate either side. Software initiatives may eventually link external OpenSpec/git/test/deployment outcomes, which remain execution truth. Planning-side `progress_evidence`, planned-versus-recorded comparison, and review cadence belong to the later Planning change.

## Risks / Trade-offs

- **[Declarative Markdown grammars can become too general]** → Ship one bounded heading/block/delimiter grammar that fits X3; refuse unsupported layouts and add adapters later rather than embedding a parser language.
- **[Legacy deterministic IDs drift when users edit natural keys]** → Mark them inferred, keep them queryable, refuse ambiguous targeted mutation, and use explicit markers for all new agent-authored blocks.
- **[Dataset serializers cannot preserve untouched bytes portably]** → Keep datasets query-only in this delivery and defer keyed mutation until format-specific preservation contracts exist.
- **[Multi-file batch publication is not power-loss atomic to concurrent lock-free readers]** → Query canonical source directly and bind results to snapshots; keep derived history/view state non-authoritative and document the existing batch guarantee instead of strengthening it falsely.
- **[Path-level policy cannot protect mixed-sensitivity rows in one file]** → Make governance granularity explicit and require file-per-item/separate collections for mixed sensitivity in v1.
- **[Raw Markdown items pollute retrieval or stale indexes survive]** → Centralize structured-only suppression and test full/current/incremental/reconcile paths with a high-cardinality fixture.
- **[One command has a broad schema]** → Keep finite actions, bounded shared arguments, strict cross-field validation, and generated schema/parity tests; do not split by adapter.
- **[Pack metadata starts acting like migrations]** → Use existing guidance only; machine-readable blueprints and activation remain deferred, and collection creation stays explicit.
- **[The real Obsidian template menu is presently misconfigured]** → Preserve templates and test ordinary expansion/insertion in fixtures; report the live `.obsidian` discrepancy rather than editing outside the governed layer.
- **[First delivery scan cost is finite]** → Cap files/items/bytes/results, return snapshot/pagination metadata, and defer an optional rebuildable collection index until measurements justify it.

## Migration Plan

1. Ship manifest/schema/adapter support without changing existing tracker behavior.
2. Existing trackers remain discoverable and inspectable at collection level and keep their current `type`, body, archive, and templates. An adjacent explicit manifest is required for item query/mutation.
3. Explicit adoption adds an adjacent `_collection.md`; it does not rewrite the canonical tracker.
4. New agent-authored log items receive explicit invisible markers. Historical items remain unmarked unless the user edits them explicitly.
5. Raw Record paths are excluded from ordinary recall by a central derived-index predicate; older Exomem versions may index them, so rollback loses no canonical data.
6. Rendered query views/exports are returned, not canonically published, and can always be regenerated. Removing the manifest returns a tracker to legacy manual use.
7. Representation migration and canonical promotion remain future explicit changes.

Rollback is code removal plus optional manifest removal. All canonical state remains ordinary Markdown/CSV/TSV/JSON and stays manually usable.

## Open Questions

No unresolved decision blocks the first delivery. Row-level policy, persistent collection indexes, richer migrations, materialized views/charts, forms/UI, and full Planning semantics are explicitly deferred rather than guessed.
