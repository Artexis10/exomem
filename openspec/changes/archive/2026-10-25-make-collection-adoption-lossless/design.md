## Context

Adoption Studio (`adoption_run.py`) drives a durable run: `start` scans, `select` materializes folder rules, `plan` hashes and previews, `apply` commits, and `finish` checks recall. `adopt.py` holds the shared planner (`plan_import_items`) and commit (`commit_import_items`).

Capture stores a file as a Source with an artifact through `add.add(..., artifact=SourceArtifact(...))`. That path files an unclassified Source in `Sources/Unclassified/` and accepts no import provenance. It writes `extracted_by: pending`, and the file watcher's reconciliation enqueues the media job.

The Source taxonomy (`source_taxonomy.py`) places a classified Source in `Sources/<Kind>/<Domain>/`, at most two levels deep. Its resolver returns `status="unregistered"` for an unknown safe key; it does not refuse.

An MCP mutation with byte-identical arguments replays its stored result for 600 seconds (`writer_lease._IMPLICIT_RETRY_TTL_SECONDS`). The MCP request budget is 50 seconds (`request_budget.MCP_REQUEST_BUDGET_SECONDS`). On Cloud, an `adopt` import unpacks into `_Imports/<import_id>/`, and the assistant runs Adoption Studio there.

## Goals / Non-Goals

Goals:
- One run imports every selected file, or refuses it with a code.
- The collection's links, embeds, tags and aliases are kept.
- Homes are chosen per folder from the registry.
- A re-run, including a Cloud re-upload, creates no duplicate.
- Large collections finish in resumable, replay-safe calls.
- `finish` reports what happened.

Non-goals:
- Entities and typed relations. They stay agent proposals, and batching their approval is follow-up work that the benchmark measures.
- Restoring an Exomem-native vault into a tenant cell. The public import API refuses `restore`.
- New media engines, and any change to the compiler.

## Decisions

### D1. Non-text files import as Source artifacts through a shared seam

`start` marks a regular file with a text-like suffix as import form `text`, and any other regular file as `artifact`. A text-like file that is not valid UTF-8 plans as `artifact`, so its bytes survive.

The artifact commit is factored out of `add.add` into one seam. Capture and adoption both call it. The seam:
- streams the bytes and the SHA-256 in bounded memory;
- writes the artifact and its `<artifact>.md` page to an exact destination that the caller supplies;
- accepts validated import provenance fields;
- enforces the deployment's artifact ceiling explicitly.

After an artifact commits, adoption enqueues extraction through the media reconciliation entry point for that path. It does not wait for the watcher. Media kind comes from the closed `media_types` set, which describes format, not meaning, so engine switches and soft-fail rules apply unchanged.

An unclassified imported artifact goes to `Sources/Imported/` beside the imported text.

Why: one artifact layout, one sidecar format, one extraction path. Alternatives rejected:
- Pointing the Source at the original in place. The original can change or vanish, and Sources are immutable.
- Calling `add.add` as is. It cannot place, label or bound an import.

Original reads retain the existing no-follow file custody through hashing and streaming. They use the physical spelling that the inventory found. Apply validates the current source binding before publication and at the batch's rollback-capable completion point. A path check followed by a separate open cannot establish custody.

### D2. Exact capture, and structure kept outside the fence

The text Source keeps the original text verbatim in its fence. The renderer emits the opening fence, a newline, the content unchanged, a newline and the closing fence. It removes no trailing whitespace. Reading back everything between the opening fence line and the final newline before the closing fence yields the content. Its UTF-8 encoding hashes to `original_sha256`.

The fence also stops the original's own front matter and `- [kind]` lines from being read as governed KB structure.

Structure is resolved in two passes over the planned set.
1. Allocate and freeze every destination.
2. Read each note's links:
   - wikilinks and embeds through the existing wikilink helper, extended in place to report the alias, the embed flag and the fragment (one helper, no second pattern);
   - CommonMark links and images to local files through the shared `markdown-it-py` adapter (`markdown_regions.py`);
   - front matter through the existing parser.

A target resolves inside the collection, in this order:
1. the exact collection-relative path;
2. a path relative to the note;
3. a unique basename;
4. a unique front-matter alias.

A fragment is dropped for resolution and kept in the rendered link. An ambiguous or missing target stays unresolved.

The Source gets a generated `## Links` section with one wikilink per resolved target, pointing at that target's frozen destination. Unresolved targets are listed as text. Wikilinks in a Source body become the graph's core `links_to` edges, so no graph change is needed. These links are untyped; typed relations stay an agent decision.

Front-matter `tags` join `imported` after the vault's tag normalization. A rejected tag is reported on the plan item. Front-matter `aliases` become `aliases`. Inline tags are recorded only if the shared parser exposes them; otherwise that is a known gap, stated in coverage.

### D3. Homes per folder, from the registry

A `select` folder rule may carry `source_kind` and `domain`. `select` resolves each once and accepts only a registered result. An unregistered or near-miss key refuses that rule with the resolver's close match, and the selection stays unchanged. The registry's existing registration path adds a kind.

Classification is part of the selection identity. A classified item targets `source_taxonomy.source_directory(kind, domain)`; other items target `Sources/Imported/`. Index and log updates are computed from the actual destinations, not from one fixed `Imported` count.

Why: the taxonomy caps depth at two (`MAX_PROJECTION_DEPTH`), so mirroring a collection's tree is ruled out. A per-folder choice turns a thousand classification decisions into a few.

### D4. A collection identity that survives re-upload

`start` takes an optional `collection` label. The default is the scanned folder's name, or the single top-level folder inside an `_Imports/<import_id>/` stage. Each import records `import_collection` and `collection_path`, the path relative to the collection root, beside `imported_from` and `original_sha256`.

`plan` reads imported Source front matter once, across `Sources/`, and builds a map from `(import_collection, collection_path)` to the latest Source. An earlier import without these fields matches by `imported_from`.
- Same bytes: the item carries `existing_ref`, and `apply` records `already-applied` with that ref.
- New bytes: the item imports as a new Source with `previous_import` set to the latest earlier Source's ref.

`apply` repeats the identity check under its mutation guard before each write, so two competing runs cannot both import an item.

### D5. Bounded calls with a continuation cursor

`plan` and `apply` each do bounded work per call: streaming hashes for `plan`, commits for `apply`. Each stops when the remaining request budget falls below a margin.

A call that stops early returns `progress` and a server-issued `continuation`. The next call passes that cursor. Because its arguments differ, the replay cache does not return the old result. A call that repeats the same cursor replays the same response, which is safe for a lost acknowledgement. The `plan_id` stays the binding.

`plan_id` hashes the complete planned actions: destinations, classification, provenance fields, the rendered link projection, and each original's hash.

In `apply`:
- Each chunk commits its text pages, streamed artifacts, indexes, log and outcomes in one `batch_atomic_write`.
- Artifact writes use the D1 seam shared with capture. Extraction requests follow the commit.
- After a crash with uncertain commit state, the D4 identity check records committed items as `already-applied`.
- A batch exception whose commit state is unknown is resolved the same way on the next call.
- A frozen destination that another writer occupied since `plan` fails with `TARGET_OCCUPIED`. It never moves, because links point at it.

The phase becomes `applied` only when every planned item has an outcome. `status` reports a budget stop as remaining work with the cursor, not as an interrupted crash. The outer mutation guard spans one bounded call, never the whole run.

### D6. Honest coverage on finish

`finish` keeps `recall_check` and `first_question`, and checks `recall_check` against the exact target path. It prefers the largest imported text item, falling back to the largest artifact when no text item exists. It adds `coverage`:
- outcomes counted by status and code;
- skipped hidden folders and junk counted by reason, with file counts;
- media states for artifact items: done, pending, engine disabled, failed, or unknown;
- resolved and unresolved link counts;
- a recall sample.

The sample takes up to ten applied items, spread over the collection's top-level folders in a fixed order. It runs a keyword `find` on each title and counts a hit only on the exact target path. Pending or unknown extraction is never reported as done.

The inventory records each hidden folder as one row with its file count and reason, instead of omitting it. The filename-token junk classifier is existing debt. It is reported, not extended.

### D7. Guidance and the staging path

The scaffold's `exomem-ingest` skill gains a section on bringing an existing collection:
- `adoption_studio` from `start` to `finish`;
- folder rules with a kind;
- following `continuation` until done;
- reading coverage;
- the Cloud route through `observe_transfer` `import_handoff` and `_Imports/<import_id>`.

The skill stays generic. The `adoption_studio` description names notes, documents, images, audio and video. The retired `_Staging/adoption` intake requirement is replaced by one for `_Imports/<import_id>/`.

## Risks / Trade-offs

- **Existing scan limit.** Studio's overview can hide a subtree reached through colon-bearing names. Stateless import keeps literal POSIX names; this batch does not relax public path filtering.
- **Disk.** Attachments are stored twice until the original is removed. The plan reports total bytes before `apply`. On Cloud, the staged folder is discarded after the import.
- **Basename ambiguity.** Two notes with one name leave links to that name unresolved. A guess would create a wrong edge.
- **Seam refactor.** Moving the artifact commit out of `add.add` touches capture. The existing capture tests must pass unchanged.
- **Tool surface.** New inputs (`collection`, `continuation`, rule classification) change the tool schema fixture. The tool-surface owner is told.

## Migration Plan

No data migration. Earlier imports stay where they are. The identity check falls back to `imported_from` and `original_sha256`, which every earlier import carries. Rollback is a release revert; imported Sources remain valid pages.
