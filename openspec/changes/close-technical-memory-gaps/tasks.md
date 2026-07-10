## 1. Transport and E2E truth gate

- [x] 1.1 Reproduce and resolve the FastMCP/Starlette lifespan hang with bounded transport-test timeouts
- [x] 1.2 Add an installed-wheel stdio MCP product-loop E2E covering the governed lifecycle and restart persistence
- [x] 1.3 Add authenticated HTTP lifecycle E2E and CI wiring for lean versus tiered heavy gates

## 2. Stable memory identity

- [x] 2.1 Implement `exomem_id` generation, canonical reference formatting, and reference parsing
- [x] 2.2 Add the rebuildable reference sidecar with duplicate/malformed ID diagnostics
- [x] 2.3 Add IDs and `ref` outputs to governed source, note, entity, and evidence-sidecar writers
- [x] 2.4 Resolve canonical/path references across read, edit, replace, connect, and review inputs
- [x] 2.5 Add dry-run-default atomic ID backfill through `maintain_memory`
- [x] 2.6 Wire moves, deletes, watcher events, audit, and reconcile to reference-index freshness

## 3. Unified graph context

- [x] 3.1 Add canonical `connect_memory(operation="context")` with the compatibility alias
- [x] 3.2 Assemble bounded blocks, graph edges, provenance, evidence, and supersession history
- [x] 3.3 Preserve unresolved relation targets as placeholder nodes
- [x] 3.4 Add context-quality golden fixtures and regression metrics

## 4. Governed schema evolution

- [x] 4.1 Define schema contract models, corpus profiling, and conservative inference
- [x] 4.2 Implement safe contract persistence with expected-hash overwrite protection
- [x] 4.3 Implement validation and corpus/contract diff
- [x] 4.4 Register `schema_memory` across MCP, REST, CLI, OpenAPI, and generated docs

## 5. Quality and compatibility gates

- [x] 5.1 Update MCP schema fixtures, tool annotations, scaffold guidance, and capability docs
- [x] 5.2 Make Ruff required after clearing the baseline and add targeted type checking for new contracts
- [x] 5.3 Run focused, full lean, E2E, and OpenSpec verification; document heavy-gate results and remaining limitations

## Verification record (2026-07-09)

- Focused identity, context, and schema regression suite: 23 passed.
- Full lean suite outside the restricted AnyIO sandbox: 1750 passed, 19 skipped.
- Installed-wheel product E2E over real stdio MCP and HTTP REST/MCP: passed in
  14.6 seconds, including restart after deleting the reference sidecar.
- Package build, generated capability check, MCP schema fidelity, repo-wide Ruff
  correctness baseline, targeted full Ruff, targeted mypy, and strict OpenSpec
  validation: passed.
- Executable Exomem-versus-Basic-Memory product benchmark: all 10 flows passed;
  4 rated ahead, 5 comparable, 1 behind, and 0 missing.
- A deliberate restricted-sandbox transport canary was terminated by
  `pytest-timeout`'s thread method at five seconds, proving hangs are bounded.
- Real embeddings/reranking remain in the existing model CI lane. Real OCR, PDF,
  ASR, CLIP, and video are wired to the scheduled/manual `heavy-media.yml` gate
  with dependency preflight and `--require-all`; that heavy lane was not run
  locally because the lean environment intentionally lacks its models and system
  dependencies.
- The lean skips are the documented optional model/media dependencies plus
  platform-specific Windows cases; they are not silent product-loop omissions.
