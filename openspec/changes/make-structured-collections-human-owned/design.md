## Context

Structured collections deliberately keep canonical state in Markdown, but their current representation is uneven. Planning and Records item identity is exposed as a UUID filename; most Planning meaning lives in frontmatter; Records presentation is an optional profile-specific recipe; typed relations rarely become Markdown links; and generated blocks can outlive the recipe that owns them. The result is structurally governed but poor in ordinary vault tools.

The substrate already has the necessary foundations: collection-scoped immutable IDs, guarded item writes, content hashes, collection inspection, Records manifest revision/rebaseline, managed presentation markers, and one registry-generated MCP/CLI/REST command plane. The change should extend those mechanics rather than create a parallel authoring system.

Two compatibility constraints matter. First, `add-readable-note-filenames` owns new ordinary compiled-note filenames; this change must reuse its cross-platform character and path sanitisation but own only structured items. Its ordinary-note path-as-address decision does not transfer here because structured items already have immutable collection-scoped IDs. Second, existing UUID paths and `record_presentation` manifests must continue to work until a user explicitly migrates them.

## Goals / Non-Goals

**Goals:**

- Make Planning and Records items understandable as ordinary Markdown files in Obsidian and filesystem browsers.
- Preserve immutable collection-scoped IDs as canonical identity while treating path, title, and generated body as projections.
- Turn typed vault relationships into real, policy-safe wikilinks so the Markdown graph reflects structured relationships.
- Keep generated presentation synchronized with canonical fields and make every representation defect inspectable.
- Give Planning the same guarded manifest lifecycle available to Records.
- Migrate existing collections through an exact preview and guarded all-or-nothing apply.

**Non-Goals:**

- Building or embedding a customer portal graph viewer.
- Replacing Obsidian, generating dashboards, or making graph topology canonical.
- Renaming ordinary Notes, Sources, or Evidence; `add-readable-note-filenames` owns the relevant ordinary-note path.
- Encoding mutable workflow state such as status, priority, health, commitment, or horizon in filenames.
- Automatically deciding when a human title should change or repairing invalid canonical items.
- Interpreting Records observations or Planning intent while rendering them.

## Decisions

### 1. Identity remains immutable; filenames become a governed projection

`plan_id` and `record_id`, scoped by `collection_id`, remain the only item identity. A new optional `item_filename` manifest recipe declares version `1` and an ordered list of schema fields. The filename renderer joins normalized non-empty values with ` — `, applies the ordinary-note filename sanitizer, and adds `.md`. If two items render to the same path, the renderer adds a deterministic short identity suffix and reports that choice in preview output.

Planning recipes may use only the plan's title-bearing natural-key field. Records recipes may use only fields declared by the collection's immutable natural key; an event kind is therefore valid only when it is part of that natural key. Manifest validation rejects mutable lifecycle, priority, commitment, horizon, health, window, and other non-identity fields even if their present values appear stable.

This is preferable to making the filename canonical because links, audit history, and concurrent guards continue to survive moves. It is preferable to putting status in the path because state transitions no longer generate noisy renames.

### 2. Renames are explicit representation maintenance

Creating a new item in a collection with `item_filename` uses the rendered path immediately. Updating a recipe field updates canonical content but does not silently move an existing file. Inspection reports `filename_drift` with the current and expected governed paths.

`maintain_memory(mode="structured-files")` is the bulk and repair surface. A preview is read-only and returns a bounded plan containing the selected collection, exact source snapshot, item moves, presentation rewrites, inbound-link rewrites, collisions, blockers, and a deterministic plan identity. Apply requires that plan identity and the unchanged source snapshot, enters writer authority once, and publishes the complete collection transformation atomically. It refuses rather than partially applying a stale or blocked plan.

The migration rewrites governed mutable inbound links and regenerates typed-link presentations. It refuses when a move would strand an inbound link in append-only, withheld, or otherwise unowned material. It does not leave UUID redirect notes, because those would preserve the implementation clutter and distort the graph.

An alternative was to rename automatically on every title update. That makes ordinary edits surprisingly high-blast-radius and can mutate files that the caller never saw, so it is rejected.

### 3. One shared presentation recipe owns readable Markdown

A new optional `item_presentation` version `1` recipe is valid for Planning and Records Markdown-item collections. It declares a title field, ordered summary fields, optional long-text fields, and typed relationship fields. The renderer emits a bounded managed block with a human heading, labelled canonical values, readable long text, and a Related section. Authored Markdown outside the managed block remains byte-preserved.

Vault-resolvable typed relationships render as `[[vault/path|human label]]` after target authorization. Opaque external execution pointers remain labelled opaque text and never become vault links. A missing, ambiguous, or withheld target uses the existing non-disclosing relationship behaviour and cannot leak a target path through the generated body.

The managed marker records the recipe digest and canonical item version. The body is therefore a deterministic projection, not a second source of truth. Every successful add, append, update, triage, or relevant manifest operation regenerates it in the same write batch. Query and graph behaviour continue to derive from canonical files.

This generic recipe is preferable to teaching Obsidian about frontmatter-only records or adding a proprietary viewer: the vault stays useful without Exomem running.

### 4. Existing Records presentation remains compatible

`record_presentation` remains accepted with its current query child-projection semantics. A manifest cannot declare both `record_presentation` and `item_presentation`. Existing Records rendering keeps its current bytes until an explicit manifest revision and structured-files migration converts it.

The shared lifecycle checks apply to both recipe forms. Removing either recipe defaults to refusal while owned managed blocks exist. A revision may request transactional cleanup; validation then proves every owned block can be removed without touching authored Markdown before publication.

This avoids a flag-day manifest conversion while closing the orphan-block defect for both old and new collections.

### 5. Inspection measures the complete representation state

Targeted collection inspection scans canonical items and managed markers even when the current manifest has no presentation recipe. It reports bounded diagnostics for expected filename drift, path collisions, missing or stale managed blocks, orphan managed blocks, unrenderable fields, unresolved typed-link presentation, and authored changes inside managed authority.

Inspection stays report-only. A healthy result means the manifest, items, audit chain, filename projection, and presentation projection agree; absence of an active recipe does not suppress orphan detection.

### 6. Planning reuses the guarded manifest lifecycle

`plan_memory` gains `validate`, `revise`, and `rebaseline` actions with the same complete-manifest, expected-manifest-hash, expected-container-hash, reason, gap acknowledgement, audit, and reader-floor principles already used by Records. Planning retains its semantic profile and plan audit vocabulary; the shared collection lifecycle leaf performs validation and publication.

Planning manifest validation additionally type-checks saved-view predicate literals against the profile vocabulary. A saved view that compares `horizon` to undeclared values such as `now`, `next`, or `later` refuses even though the underlying field is syntactically a string. This catches a view that can never match canonical Planning values before it reaches a live vault.

Reusing the lifecycle is preferable to editing manifests through generic file operations, which would bypass the audit and stale-write contract.

### 7. Representation maintenance is profile-neutral and registry-generated

The structured-files maintenance leaf resolves the collection manifest and then delegates to the Planning or Records adapter. Its preview/apply schema and result envelope are registered once and projected byte-consistently to MCP, CLI, REST, OpenAPI, and capability guidance. Preview is classified read-only; apply is classified mutating.

No server-side reasoning is introduced. Filename generation, recipe rendering, link resolution, collision detection, and drift comparison are deterministic substrate operations.

## Risks / Trade-offs

- **Large migrations touch many files and inbound links** → Preview lists every move and rewrite, apply requires the exact unchanged snapshot, and publication is one guarded batch per collection.
- **Human-readable names can collide or exceed platform limits** → Reuse one sanitizer, cap path bytes, add a deterministic identity suffix, and expose the final path before apply.
- **A raw or withheld note may link to an old UUID path** → Refuse the move as blocked rather than mutating append-only material or silently breaking the link.
- **Generated Markdown may be mistaken for canonical authored prose** → Use explicit managed markers and recipe/item digests; preserve authored Markdown outside the block exactly.
- **Refreshing links could disclose a governed target** → Resolve and authorize before rendering and use the existing indistinguishable missing/withheld refusal behaviour.
- **Two presentation recipe forms increase temporary complexity** → Keep `record_presentation` compatibility read-only in scope, forbid both forms together, and provide an explicit conversion path.
- **Planning action growth expands public schemas** → Generate every surface from the existing registry and update schema-fidelity fixtures intentionally.

## Migration Plan

1. Add shared filename/presentation schemas, renderer, diagnostics, and Planning lifecycle support without changing existing manifests or paths.
2. Ship read-only structured-files preview and verify exact plans against representative Planning and Records fixtures, including collisions and blocked inbound links.
3. Ship guarded apply and compatibility conversion for `record_presentation`.
4. Revise one collection manifest at a time, preview its representation migration, review blockers, and apply only against the unchanged snapshot.
5. Validate with collection inspection and an Obsidian graph/readability smoke test; the graph is acceptance evidence, not a new canonical output.

Rollback before migration is code-only because existing collections are untouched. After an applied migration, rollback uses the committed audit receipt's inverse file/link plan against the exact post-migration snapshot; it never guesses prior paths or body bytes.

## Open Questions

None. The implementation may refine bounded constants, but identity, lifecycle, migration, compatibility, and authority decisions are fixed by this design.
