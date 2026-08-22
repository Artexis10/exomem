## Context

Markdown-item Records already combine typed YAML frontmatter with an optional Markdown body. Nested object arrays are lossless but unreadable in Obsidian's property UI. `expand_children=true` also iterates adapter-owned `Record.children`, which Markdown-log populates and Markdown-item leaves empty.

Frontmatter must remain authoritative, direct edits visible, reads non-mutating, and audit publication atomic. This change depends on the completed `make-records-first-class-and-recoverable` nine-action contract and must archive after it is synced.

## Goals / Non-Goals

**Goals:** readable nested Records in ordinary Obsidian; one file and one canonical value representation per parent; safely paginated explicit child arrays; byte-preserved prose and auditability.

**Non-Goals:** standalone YAML; one file per child; a canonical schema for arbitrary child keys; reverse-parsing Markdown; Planning presentation; domain interpretation; silent repair; storage migration.

## Decisions

### YAML stays first-class inside Markdown

The `.md` file remains the Record. Frontmatter is the sole canonical structured authority; the body may contain authored prose and a deterministic managed projection. Standalone YAML would split prose/navigation from data without solving tables. Per-measurement files would add hundreds of noisy sync/navigation units.

### Reserve a Records-only `record_presentation` recipe

The optional closed namespaced key is valid only for Records `markdown-items`; legacy custom `presentation` data stays opaque. Version 1 contains bounded summary descriptors and `table`, `notes`, and `details` sections. Parent references use binding-schema fields. A table names one array-of-object field and declares child columns with exact field, optional label, scalar type, and optional link kind.

Columns are not a second canonical schema. They are the typed allowlist for body rendering and governed query egress. Undeclared child keys never enter either. Persisted rendering serializes canonical link literals deterministically and is protected by whole-file authorization. Query egress dynamically authorizes link columns before every observable query operation. A projection type mismatch is `unrenderable`; derived output refuses and guarded canonical value update is the remedy. This keeps older writers compatible without adding a reader floor.

Planning is excluded because only Records has the specified refresh surface.

### Store one exact derived block

The renderer emits one bounded versioned HTML-comment span with SHA-256 over canonical JSON of recipe identity and selected canonical values. It never parses back into values. Source-byte splicing preserves everything outside the exact span, including BOM/newline/body-separator/leading blanks/final-newline shape and marker-like ordinary prose.

Append/value update renders automatically. Guarded `update(refresh_presentation=true)` can backfill with no value changes. Query, inspect, revise, rebaseline, and reconcile never write it. All rendering mutations refuse ambiguous/malformed/oversized markers.

Inspection returns only non-current released items, sorted by item key, with state totals/truncation. Entries contain item key, path, version, and `missing|stale|malformed|unrenderable`; safe unrenderable locations may name table/column/child index. Presentation-only findings retain lifecycle guards. Direct selected-value edits use `inspect → rebaseline → reacquire guards → refresh`.

### Safe nested projection applies before all governed query modes

Before filtering, sorting, aggregation, pagination, rendering, or returning a query, each `record_presentation` table array is reduced recursively to declared type-valid columns and audience-projected links. Unexpanded parent rows therefore never expose raw open child objects.

`expand_child` names a Markdown-item table field or Markdown-log container. Expanded rows contain parent fields except the container, system/parent identity, `child_field`, `child_index`, and the same safe child projection. A hard total child cap precedes materialization. `expand_children=true` remains sugar only for one eligible container/table; ambiguity, open objects, datasets, or both selectors refuse.

### Semantic body identity excludes one valid managed span

Regeneration changes exact item/container hashes and emits an ordinary audit event. Semantic append replay hashes canonical values plus exact authored-body bytes with one valid managed block removed. Renderer-version changes cannot create identity conflicts; malformed/tampered blocks are never stripped. Receipts/item versions still bind exact complete bytes.

## Risks / Trade-offs

- [Table appears authoritative] → Generated label/digest, no reverse parsing, exact inspect comparison.
- [Nested values leak] → Safe projection applies to expanded and unexpanded queries before all operations.
- [Policy changes alter files] → Stored links are deterministic; only query egress is audience-specific.
- [Current items crowd out stale repair state] → Return only non-current entries with counts/truncation.
- [Old writer violates projection type] → Report `unrenderable`, refuse derived output, require guarded value repair.
- [Markers collide with prose] → Recognize exact delimiters only and refuse ambiguity.
- [Large arrays consume memory] → Pre-materialization total cap plus page/byte caps.

## Migration Plan

1. Land additive parser/renderer/diagnostics/refresh/query support.
2. Existing manifests/items without `record_presentation` stay byte-identical.
3. Guardedly revise a Records manifest to opt in; inspect lists missing blocks.
4. Refresh existing items; new append/update renders automatically.
5. After direct selected-value edits, rebaseline history, reacquire guards, then refresh.
6. Older builds ignore the optional key/body and continue reading canonical frontmatter.

## Open Questions

None.
