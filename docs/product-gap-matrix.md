# Product gap matrix: Exomem vs Basic Memory

Generated from local product-flow benchmarking on 2026-07-09.

This is a product benchmark, not an internals comparison. Basic Memory was inspected only through public/product-facing surfaces in the sibling checkout: README/docs/tool names. No Basic Memory implementation or benchmark code was copied.

Run locally:

```powershell
uv run python scripts/product_flow_benchmark.py
uv run python scripts/product_flow_benchmark.py --json
uv run python scripts/product_flow_benchmark.py --flow fresh_setup --flow search_recall
```

The harness creates temporary vaults under `.pytest-tmp/product-flow-benchmark/` by default and removes them unless `--keep-tmp` is set.

## Verdict

Exomem is stronger where the product needs governance: source/evidence separation, provenance, review queues, and safe copied-source preservation. Basic Memory is stronger where the product needs low-friction adoption: cloud/no-install path, sync, importers, schema tools, multi-project UX, context/canvas workflows, and packaged assistant ecosystem.

The earlier largest product gap was first-run adoption: direct CLI scans of a messy uninitialized vault failed before they could report anything useful. That is now fixed for the product surface: `browse_memory(mode="overview")` and `adopt_vault(mode="scan-only")` can inspect a pre-init vault, while write-capable adoption modes still require an initialized governed KB.

## Matrix

| Flow | Exomem rating | Harness status | Basic Memory comparison | Evidence / gap |
| --- | --- | --- | --- | --- |
| Fresh vault setup | Behind | Pass | Basic Memory has simpler public local install (`uv tool install basic-memory`) and cloud/no-install onboarding. | Exomem `setup --yes --lean --no-hooks --skip-claude-register` works and initializes the scaffold, but still feels like configuration rather than immediate product use. |
| Existing messy vault adoption | Comparable | Pass | Basic Memory has importers and project setup paths that are more discoverable. | Exomem's product model is safer: `browse_memory` and `adopt_vault(mode="scan-only")` scan before initialization, and write modes preserve originals under governed source/adoption folders. |
| Search / recall | Comparable | Pass | Basic Memory has polished text/vector/hybrid search docs and richer search-result UX. | Exomem `ask_memory` recalls seeded notes with vault-relative cited paths. Lean benchmark uses keyword mode to avoid model downloads. |
| Write / remember | Ahead | Pass | Basic Memory `write_note` is easier; Exomem is more governed. | Exomem writes raw Sources and compiled notes separately, canonicalizes the source citation, and updates the source `ingested_into` backlink. |
| Source preservation | Ahead | Pass | Basic Memory importers cover more formats. | Exomem `adopt_vault(mode="copy-as-sources")` preserves the original file, records `imported_from`, SHA-256, and byte count. |
| Evidence / provenance | Ahead | Pass | Basic Memory is primarily note/knowledge-graph oriented. | Exomem has an explicit Evidence tree plus `review_memory(mode="provenance")` over durable `<!-- key:value -->` markers. The marker workflow is still hidden. |
| Schema inference / validation | Missing | Not measured | Basic Memory publicly exposes `schema_infer`, `schema_validate`, and `schema_diff`. | Exomem validates its own page types but has no comparable user-facing schema inference/validation flow. |
| Graph / context building | Behind | Pass | Basic Memory exposes `build_context` and `canvas` as clearer user/assistant workflows. | Exomem can do inbound links, `ask_memory(deep=true)`, and `connect_memory(operation="suggest-links")`, but Basic Memory's context/canvas story is still easier to explain. |
| Review / stale / contradiction workflow | Ahead | Pass | Basic Memory has recent activity and graph navigation, but less explicit epistemic review. | Exomem `review_memory` surfaces unprocessed source work, audit findings, attention queues, provenance, and compilation scaffolds. |
| Assistant onboarding | Comparable | Pass | Basic Memory has broader packaged plugins, skills, and public docs across clients. | Exomem `bootstrap` exposes front-door actions and product commands; `demo --json` proves doctor/retrieval/review against a sample vault. |

Current harness summary: 10 flows; 4 ahead, 3 comparable, 2 behind, 1 missing. Status: 9 pass, 1 not measured.

## Prioritized backlog

1. **Keep first-run adoption polished across surfaces.** The CLI now lets `browse_memory(mode="overview")` and `adopt_vault(mode="scan-only")` scan a raw messy vault before `Knowledge Base/_Schema/SKILL.md` exists. Keep MCP and REST aligned with that contract, and make the safe next action obvious.

2. **Collapse onboarding into one obvious first action.** Keep `setup`'s safety, but reduce the user's mental model to: demo, choose vault, scan, initialize. Basic Memory is ahead because its first-run story is easier to explain and it has a cloud escape hatch.

3. **Make graph/context a single product story.** Exomem has good pieces (`ask_memory(deep=true)`, inbound links, suggestions), but Basic Memory's `build_context`/canvas story is easier to use. Document the front-door `connect_memory`/deep-context flow so it returns the relevant notes, links, and surrounding graph.

4. **Decide whether schema inference belongs in Exomem.** Basic Memory is plainly ahead here. If Exomem wants this capability, expose it as a governed validation/audit flow, not as a Basic Memory clone.

5. **Make evidence/provenance discoverable.** The Evidence tree and `review_memory(mode="provenance")` are a real differentiator, but the HTML-comment marker workflow is too implicit. Add assistant guidance so users do not need to know the comment syntax.

6. **Polish write ergonomics without dropping governance.** `capture_source` + `remember --field sources=...` is safer than a plain note write, but heavier. The product remember flow should guide the assistant through Source vs Note vs Evidence without requiring the user to name page types.

## Harness coverage notes

- The benchmark intentionally runs real Exomem CLI commands through subprocesses.
- Heavy embedding/media paths are disabled so the harness stays local, fast, and deterministic.
- `schema_inference_validation` is rated `missing` rather than failed because there is no Exomem command to run.
- `messy_vault_adoption` covers the fixed pre-init scan path and confirms scan-only leaves originals untouched.
- The Basic Memory reference detector reads public-facing README/docs text and records observed product promises; it does not execute or copy Basic Memory code.
