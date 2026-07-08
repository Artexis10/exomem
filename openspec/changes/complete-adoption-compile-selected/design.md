# complete-adoption-compile-selected - design

## Context

`adopt` already gives a bounded scan, a governed manifest, and explicit source copies with original path and SHA-256 provenance. `compile-selected` is advertised as planned, and `propose_compilation` already produces read-only note scaffolds from governed Sources. The missing piece is the adoption bridge: selected legacy files should be normalized into governed source refs and handed to compilation planning without rewriting originals or auto-writing compiled notes.

## Goals / Non-Goals

**Goals:**
- Make `adopt(mode="compile-selected")` an implemented, selected-path-only planning mode.
- Preserve the current non-destructive adoption contract and reuse existing safe copy/source provenance.
- Return stable refs for originals, copied sources, and compile proposals so agents can review and cite the plan.
- Keep the same leaf function across MCP, CLI, and REST.

**Non-Goals:**
- No automatic creation of `Notes/...` compiled pages.
- No bulk migration, restructure, frontmatter insertion, deletion, or moves outside `Knowledge Base/`.
- No server-side reasoning LLM. The server prepares deterministic scaffolds; the agent/user decides the final note.

## Decisions

1. **`compile-selected` is planning, not writing compiled knowledge.**
   It calls the existing read-only `propose_compilation` helper after selected legacy files are represented as governed Sources. The compiled page is still created later through `note`.

2. **Legacy selected files are copied using the existing `copy-as-sources` path.**
   This keeps path resolution, text suffix filtering, hash provenance, index updates, and log entries consistent. The mode returns skipped items instead of widening selection implicitly.

3. **Stable context refs are metadata, not a replacement path API.**
   Add a small helper that formats `exomem://vault/...`, `exomem://source/...`, and `exomem://manifest/...` refs. Existing vault-relative and KB-relative paths remain authoritative for tool inputs.

4. **Already-governed Sources can be planned directly.**
   If a selected path is under `Knowledge Base/Sources/`, skip copying and pass the canonical source path to `propose_compilation`. Other `Knowledge Base/` paths are skipped because adoption is for legacy/source material, not compiled-page rewrites.

## Risks / Trade-offs

- **Duplicate source copies on repeated compile planning** -> Accept for now; the copied source carries original path/hash provenance and `unique_path` prevents overwrite. Deduplication can be added later without changing the public contract.
- **Proposal text is scaffold quality, not final knowledge** -> Mitigated by naming the output `compile_plan`/`proposal` and documenting that `note` is the deliberate write step.
- **Tool schema drift** -> Update the committed MCP schema fixture if `op_adopt` description/signature changes.

## Migration Plan

No migration is required. Existing manifests and imported sources remain valid. New runs of `adopt(mode="compile-selected")` write only governed source copies for legacy selections and return read-only compile plans.

## Open Questions

(none)
