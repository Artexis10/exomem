## 1. Source-Closure Contract

- [x] 1.1 Add failing pure-logic tests for absent/empty sources, current-path and stable-ref closure, external locator refusal, mixed resolved/unresolved lists, deterministic caps, and indistinguishable missing/withheld/ineligible outcomes.
- [x] 1.2 Implement a shared authorization-aware source-closure validator that accepts only eligible governed Source or Evidence material and returns the stable bounded refusal data.
- [x] 1.3 Register `UNRESOLVED_SOURCE_CITATION` with deterministic capture-first remediation and shared application-envelope projection.

## 2. Semantic Writer Integration

- [x] 2.1 Add failing creation and complete-replacement tests proving every explicit source is checked inside writer authority and a refusal writes no note, back-reference, index state, or committed receipt.
- [x] 2.2 Integrate source closure into the shared compiled-note precommit path used by `remember_memory`, `replace_memory`, and governed Tier-2 creation; remove the warning-only compile-then-capture path.
- [x] 2.3 Add failing edit tests proving source-changing patches validate the complete final list while unrelated patches to legacy unresolved notes remain allowed.
- [x] 2.4 Integrate prior-versus-final source comparison into `edit_memory` without treating a complete replacement as a grandfathered edit.
- [x] 2.5 Add failing idempotency and concurrency tests for retry after capture, source version changes, source relocation by stable ref, authorization changes, and all-or-nothing publication.
- [x] 2.6 Publish supported source back-references and the derived note in one guarded batch, rechecking source versions immediately before commit.

## 3. External Provenance and Guidance

- [x] 3.1 Add failing capture/compile tests proving connector IDs, remote file IDs, and URLs remain provenance metadata on captured material and cannot directly satisfy a derived `sources` entry.
- [x] 3.2 Ensure Source and Evidence capture preserve current raw-content and origin contracts while compiled writers remain network-free and never invoke capture implicitly.
- [x] 3.3 Update generic scaffold guidance and writer descriptions to teach capture first, governed citation second, honest empty sources, and original-only remediation without personal examples.

## 4. Legacy Audit

- [x] 4.1 Add failing audit tests for deterministic `unresolved_source_citation` findings, bounded values/counts, authorization safety, all-category inclusion, and no duplicate generic `broken_wikilink` finding.
- [x] 4.2 Implement the read-only audit category using the shared closure semantics without creating review state or mutating Notes, Sources, or Evidence.
- [x] 4.3 Add tests proving the category is absent from default attention, clears after original capture plus citation update or explicit citation removal, and remains for a merely similar derivative or unrelated source.

## 5. Product-Surface Parity

- [x] 5.1 Wire source closure and the audit category through the canonical command registry so MCP, CLI, REST, OpenAPI, bootstrap guidance, and generated capability artifacts share one contract.
- [x] 5.2 Add cross-surface tests for normal MCP application results, identical REST/CLI JSON data, human CLI rendering and exit status, Tier-2 coverage, and absence of hidden resolver details.
- [x] 5.3 Regenerate the golden MCP schema fixture with an explicit intentional-change note limited to affected writer and audit descriptions or schemas.
  - Intentional MCP delta: description-only changes for `remember`, `edit_memory`, `replace_memory`, and `review_memory`; description and input-schema changes for `plan_memory` and `maintain_memory`. No other discovered tool changed.

## 6. Acceptance and Delivery

- [x] 6.1 Add a generic end-to-end provenance fixture containing a captured original, a valid derivative, a legacy unresolved derivative, and a partial derivative that MUST NOT be promoted as the original.
- [x] 6.2 Prove capture-then-cite succeeds atomically, cite-before-capture refuses cleanly, legacy audit reports the gap, unrelated edit remains possible, and no automatic reconstruction occurs.
- [x] 6.3 Run focused note, semantic-writer, source, audit, command-surface, governance, writer, scaffold leak, and idempotency tests, then `ruff check`, the full embeddings-disabled pytest suite, and `openspec validate --all --strict`.
  - Verified on 2026-08-26 with the final tree: focused source-closure and semantic-writer suites, four deterministic embeddings-disabled pytest shards, the isolated 20-test bounded-graph timing file, required Ruff checks, generated-artifact checks, public-artifact validation, and 168/168 strict OpenSpec validations passed.
