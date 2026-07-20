## Follow-up change boundaries

This change closes mutation acknowledgement and replay correctness. The remaining ChatGPT ergonomics work is intentionally split so response-shape and tool-schema churn cannot obscure the incident fix.

### B. Compact mutation terminal envelope

Proposed change: `compact-mutation-terminal-envelope`.

- Lead every mutation response with `ok`, `status`, `mutated`, primary `path`, `request_id`, replay disposition, and warning count.
- Keep semantic/index detail under `diagnostics` or `detail="full"`; do not discard it.
- Preserve exact replay semantics by storing and replaying the compact terminal envelope plus its diagnostics reference.
- Add conformance tests for committed, replayed, pre-commit busy, pending acknowledgement, committed-uncertain, and full-detail responses.

This correctness change implements the structured error half (`status`, `committed`, `request_id`, `receipt_id`, reusable explicit key when present) but does not change successful leaf return shapes.

### C. Discriminated edit schema and capability conformance

Proposed change: `discriminate-edit-memory-and-filter-bootstrap`.

- Replace the optional-field matrix with a discriminated operation union for `replace_body`, `replace_string`, `edit_section`, `patch_frontmatter`, and `fill_row`, or expose those as focused tools.
- Generate each schema so unrelated fields are absent, and return mode-specific local validation guidance.
- Derive bootstrap recommendations from the actual exported `tools/list` for each profile/surface, or export every advertised command.
- Add schema-negative tests and bootstrap-versus-tool-surface conformance tests for ChatGPT, compact MCP, full MCP, REST, and hosted profiles.

### D. Action-first audit and bounded reranking

Proposed change: `triage-audit-output-and-bound-reranking`.

- Sort current non-grandfathered blockers first, malformed/unregistered relations next, and legacy/grandfathered disposition debt into grouped backlog counts.
- Return representative samples by default and require an explicit full-enumeration option.
- Prove a small current blocker set remains visible beside hundreds of legacy findings.
- Keep reranking opt-in; add a candidate cap and caller latency budget with timing tests around the rerank stage.

The present change only prevents `maintain_memory(mode="audit")` from retaining the hosted mutation boundary. It does not alter audit finding severity or retrieval ranking.
