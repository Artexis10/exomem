## Context

The collection parser already owns a strict deterministic contract, and collection manifests remain the binding human-editable representation. The missing layer is introspection: clients currently receive one opaque text field and must infer parser requirements from failed mutations. An empty vault makes that especially acute because there is no first-class manifest to inspect as an example.

The ordinary user experience should remain product-shaped. Parser grammar, JSON Schema, and worked manifests are agent instructions and should appear only through an explicit agent-facing request.

## Goals / Non-Goals

**Goals:**

- Make the complete Records manifest contract discoverable without repository or skill access.
- Make create preflight genuinely read-only and reuse the same parser and create-only checks as commit.
- Let a client inventory available collections before it knows a selector.
- Preserve stable validation codes while making closed-enum failures self-remediating.
- Prove the full empty-vault workflow deterministically.

**Non-Goals:**

- Add a second structured `collection_spec` authoring model or a YAML renderer.
- Add hidden domain storage engines, automatic migrations, or automatic collection activation.
- Constrain the existing open lifecycle string to a new enum.
- Change append/update semantics, canonical file formats, audit history, or governance rules.

## Decisions

### Add explicit `describe` and `validate` actions

`describe` returns a deterministic, content-free contract containing the manifest filename, versioned JSON Schema, closed enums, query/view vocabulary, exact non-enum constraints, and two canonical manifests. The complete example models a generic laboratory panel with nested analytes and provenance, without medical interpretation or private data.

`validate` accepts `manifest_path`, `manifest_text`, and optional `scaffold`. It requires no mutation rationale because it never writes. It parses through the same binding parser, enforces the Records profile, checks safe paths and create-only conflicts, validates scaffold requirements, and returns the normalized contract plus `would_create` paths and warnings. Race-sensitive guards are repeated inside the later create mutation.

Alternative considered: `create(validate_only=true)`. Rejected because the command registry already classifies safety by the finite action selector; a separate action makes read-only lease and hosted admission mechanically unambiguous.

### Keep one canonical authoring representation

Create continues to accept complete human-owned Markdown/YAML. The returned JSON Schema and examples make that serialization discoverable without creating a parallel object-to-YAML renderer whose behavior could drift from the parser.

Alternative considered: add `collection_spec` now. Deferred until usage demonstrates that a second authoring representation is worth its compatibility and parity burden.

### Make collection-less inspection an inventory

Targeted inspection is unchanged. Omitting `collection` returns a bounded, governance-filtered inventory. First-class manifests are discovered through the existing safe manifest scan. Legacy candidates are scanned only under the exact Records layer, authorized before parsing, capped, and projected without item contents.

### Keep bootstrap concise

Compact bootstrap reports Records availability, all finite actions, the `_collection.md` filename, supported collection/profile versions, and the canonical `describe -> validate -> create -> inspect -> append` route. It does not embed the JSON Schema, parser field table, or worked manifest. Full technical guidance remains opt-in through `describe`.

### Attach remediation facts to parser errors

Collection errors may carry a bounded details object. Closed enums return `field`, `received`, `allowed`, and `example`; missing required fields return the exact field, expected shape, and example. Existing codes and human-readable messages remain stable.

## Risks / Trade-offs

- **Contract drift between parser and describe output** -> define the public contract beside parser constants and add parity tests for every closed enum and canonical example.
- **Inventory leaks governed paths** -> authorize each candidate before parsing or projection, bound scans, and keep denied candidates indistinguishable from absence.
- **Validation is mistaken for a reservation** -> return `valid_at` semantics as a read-only snapshot and repeat all create-only and path guards at commit.
- **Bootstrap grows again** -> keep only a bounded summary and test that complete examples appear only in `describe`.

## Migration Plan

1. Ship the expanded selector and regenerated tool schema together.
2. Existing five-action callers continue unchanged.
3. Generic clients adopt `describe -> validate -> create` for new collections and collection-less `inspect` for inventory.
4. No vault migration or manifest rewrite is required.

## Open Questions

None for this repair. Structured `collection_spec` creation remains a possible separately specified follow-up.
