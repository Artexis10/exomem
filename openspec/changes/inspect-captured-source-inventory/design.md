## Context

See the proposal for the cross-service problem.
The corpus publisher already retains immutable page metadata, identity resolution and a freshness caption.
The embedding owner already publishes rows atomically, names the active recall space and records per-path generations.
Neither queue absence nor a request timeout proves that a capture is indexed or cannot commit later.

The existing whole-namespace authority protects identity capture and passage search.
Cloud credentials already bind requests to the configured cell before a command leaf handles content.

## Goals / Non-Goals

**Goals:**

- Discover the configured cell without sending a credential or tenant content.
- Traverse canonical captures with bounded work and explicit completeness.
- Report exact indexing evidence without reading every Source or vector on each request.
- Preserve ordinary capture, writer fencing and accepted-mutation replay behavior.

**Non-Goals:**

- Public account-to-cell routing, a second catalogue, inferred Source identities or automatic activation.
- Index completeness inferred from queued work, row counts alone or historical successful replies.
- A wall-clock guarantee that an accepted mutation cannot commit after its caller stops waiting.

## Decisions

### D1. Register content-free discovery on Cloud cells

Register `GET /.well-known/exomem-cell` after composition creates the active surface descriptor.
Keep the route implementation in `cell_identity.py` and the registration hook in `server.py`.
The route returns configured cell identity, software identity, implemented operations and their effective bounds.
It accepts no tenant selector, credential or content and reads no vault page, model or indexing queue.
The existing authenticated command boundary remains responsible for every content operation.

Derive operations from the composed registry and descriptor; do not maintain another command allowlist.
Derive passage and page bounds from their owning implementation constants.
Omit an unimplemented operation, including capture settlement, rather than advertising a future capability.
Return an unavailable or unknown recall space when its published identity is unavailable; never load an encoder for discovery.
Return the opaque digest of the published space rather than exposing model names or possible local paths.
Passage responses return that same `recall_space_id` alongside their existing authenticated space description.

Software identity names the package version and a digest of the package files observed at server composition.
Hash relative paths and file bytes in deterministic order, excluding interpreter bytecode and its cache directories.
The digest identifies application code and packaged data, not model weights, mutable vault configuration or an external attestation.
Cache this immutable composition observation; return its observation scope with the digest.
A version alone cannot distinguish a source overlay from another build with the same version.
This small fingerprint avoids a new build service or installation registry.

The new route is explicit service discovery, with no model or automatic mutation role.
Its public fields exclude paths, hostnames, principal identifiers, tokens and all captured metadata.

### D2. Publish canonical capture metadata with the corpus generation

Derive declaration metadata from original page bytes before provenance masking.
`SemanticPageState.body` is a rendered semantic view and cannot prove exact passage text.
Retain identity, capture digest, Source reference and declared-passage chunk expectations in immutable published metadata.

Reuse the canonical companion classifier from `source_capture.canonical_pages`.
Factor its pure classification step over publisher-owned snapshots instead of repeating the scan or implementing another classifier.
Bound Markdown originals remain excluded even if they contain forged identity or declaration fields.
Incomplete companion classification makes the affected publication incomplete.
Retain classification inputs for every Markdown page in the raw lanes, independently of semantic scan eligibility.
A semantically excluded directory cannot remove a canonical capture or companion from those inputs.
Retain hashes, declaration fields and binding dependencies rather than another full copy of every body.
When an owner or target changes, reclassify its affected binding component, including unchanged dependents.
Deleting a companion owner can make its unchanged original canonical.
Do not rescan or rehash every Source after each small publication.

Build sorted identity buckets for each system and each system-record pair during publication.
Patch them with the existing copy-on-write corpus update.
The request uses one bucket and keyset selection; it never iterates the whole corpus to select a page.
No independent persistent catalogue or filesystem watcher is added.

### D3. Inventory requires complete publication proof

Expose one governed inventory leaf through the shared MCP, CLI and REST registry.
Require existing whole-namespace content authority before returning identities or Source references.
Accept one identity system, an optional record, an opaque keyset cursor and a limit from 1 to 200.
Order by system, record, revision and canonical Source reference to make traversal deterministic.
Bind a cursor to the selected scope and reject malformed or foreign-scope cursors.

Each reply names the observed incarnation-qualified generation and whether that observation is complete.
Reuse the file freshness instance, generation and caption, or the corresponding pinned store identity and revision.
An integer generation alone can repeat after restart and cannot identify a traversal.
File mode requires a live, current full freshness caption with no unpublished external edits.
Store mode requires the pinned store revision and ownership proof for all included pages.
Unproved state returns unknown or pending, never a successful empty inventory.
This stronger absence proof is separate from passage ranking, which verifies its selected Source bytes.

Traversal is live keyset traversal, not a multi-request snapshot.
Each page reports its generation so reconciliation can detect a changed traversal and repeat its bounded pass.
Duplicate or missing identities during a changing traversal cannot complete reconciliation.

### D4. Exact indexing state comes from atomic publication receipts

Retain a compact volatile receipt when `EmbeddingIndex.upsert_batch` commits its exact chunk rows.
The embedding owner retains path, exact ordered chunk-text digest, active recall space and publication token.
The publisher retains the canonical Source witness, capture digest and expected ordered chunk-text digest.
Inventory joins those witnesses; the generic embedding owner learns no capture identity or taxonomy.
Retain no duplicate passage text or vectors in the receipt.

For one inventory page, check all retained receipts in one SQLite read snapshot.
Use the existing path-change log and set-based queries, not one query for each returned Source.
Exact counts are valid only when the transaction receipt and unchanged-path proof both remain current.
A count alone cannot distinguish incorrect text or same-sized replacement rows.

Expose `indexed`, `not_yet_indexed`, `failed`, `unknown` or `not_applicable` as fixed protocol states.
Return nullable counts and revision-qualified failure evidence; unavailable reads never become zero.
Use `indexed` only when every declared chunk is bound to the current Source and active space.
Use `not_applicable` when a canonical capture declares no passages.
Use `failed` only for a failure bound to that Source revision; an old error cannot describe a later revision.

Cold or lost receipts report unknown and enqueue selected paths for proof-only reconstruction.
Extend the existing bounded semantic-drain worker with a deduplicated volatile proof set; add no persistent queue or scheduler.
Share its work and elapsed limits, with a fair proof slot beside mutation debt before the store-mode early return.
Bind queued paths to their expected Source and recall-space witnesses and discard stale work.
Retain compact receipts for active declared Sources and retire them on purge or invalidation.
The existing 64-entry proof LRU cannot cover a 200-item page and is not this receipt store.

Reconstruction reads canonical bytes through the active storage mode and checks exact chunk text off the request path.
Extract the smallest shared exact-row verification helper from the embedding owner.
Do not call synchronous `publication_ready` from inventory or reuse projected-file reconstruction unchanged in store mode.
Do not call a preparation helper that can encode.
Reuse existing publication verification and store stamps without loading a model.

### D5. Bound and prove the new request work

The passage reference uses 1,500 Sources and at least 40,000 chunks on a 2-CPU cell.
Inventory must also remain bounded as that population grows: at most 200 returned items, one bucket seek and a constant number of SQL statements.
Record the inventory reference once in the request-latency contract: 20,000 captured Sources, at most 200 items and p95 below 200 ms.
This is a target until measured, not a reported result.
Use the same public boundary instrument before and after, with generated data and both storage modes where supported.
The count of database calls must not grow with the page length.

### D6. Capture settlement remains a separate blocked dependency

Current accepted mutations may commit after the caller's deadline, and keyed replay returns their outcome.
Keep that contract.
P2 cannot advertise a latest commit deadline under the existing writer implementation.
Its dependent tasks remain not ready until a primitive excludes delayed pre-admission requests and proves terminal settlement under the canonical writer boundary.
That decision must update `durable-source-capture` and the consuming convergence contract before implementation.
Identity and inventory implementation can proceed independently.

### D7. Reuse the existing operation and authority model

The primary-source roster's tool-interface and containment rows guide this boundary.
[Writing effective tools](https://www.anthropic.com/research/writing-tools-for-agents) supports one coherent inventory operation with meaningful, bounded results.
[Containment](https://www.anthropic.com/research/how-we-contain-claude) supports enforcing effects at the runtime boundary.

Existing registry dispatch and whole-namespace authority already implement those principles.
The new work clarifies incomplete observations and reuses publication evidence.
It adds no agent framework, scheduler, semantic classifier or human review gate.

The closest existing options are canonical capture lookup and embedding-parent publication checks.
The gap is bounded multi-Source observation with complete publication proof.
The maintenance cost is a small immutable inventory view, volatile receipt bookkeeping and one set-based validation path.

## Risks / Trade-offs

- A cold publication cannot prove absence. Return unknown and let existing publication recovery progress.
- A lost receipt delays readiness. Reconstruct it in background instead of guessing from counts.
- A changing corpus invalidates a traversal. Report generations so reconciliation repeats before it claims convergence.
- Public discovery exposes software and operation metadata. Keep every field content-free and omit private deployment details.
- Shared publisher changes can conflict with other work. Coordinate exact seams and integrate the current owner changes before delivery.

## Migration Plan

1. Ship discovery and inventory through ordinary reviewed releases; existing capture and retrieval remain available.
2. Verify identity, bounded traversal and exact indexing on isolated generated cells; record unsupported modes explicitly.
3. Connect the consumer only after its required capabilities are present; keep activation under its existing authority.
4. Roll back the release if needed; these derived views add no canonical migration or Source rewrite.
