# Product gap matrix: Exomem vs Basic Memory

Generated from local product-flow benchmarking on 2026-07-08.

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

The most important finding is not philosophical: **Exomem's direct `overview` and `adopt` CLI paths currently fail on a messy uninitialized vault before they can scan it**, even though setup uses the adoption scan internally. That makes the existing-vault story weaker than the product model promises.

## Matrix

| Flow | Exomem rating | Harness status | Basic Memory comparison | Evidence / gap |
| --- | --- | --- | --- | --- |
| Fresh vault setup | Behind | Pass | Basic Memory has simpler public local install (`uv tool install basic-memory`) and cloud/no-install onboarding. | Exomem `setup --yes --lean --no-hooks --skip-claude-register` works and initializes the scaffold, but still feels like configuration rather than immediate product use. |
| Existing messy vault adoption | Behind | Partial | Basic Memory has importers and project setup paths that are more discoverable. | Exomem's product model is safer, but direct `overview`/`adopt --mode scan-only` reject a pre-init vault because the registry CLI resolves only initialized vaults first. |
| Search / recall | Comparable | Pass | Basic Memory has polished text/vector/hybrid search docs and richer search-result UX. | Exomem `find` recalls seeded notes with vault-relative cited paths. Lean benchmark uses keyword mode to avoid model downloads. |
| Write / remember | Ahead | Pass | Basic Memory `write_note` is easier; Exomem is more governed. | Exomem writes raw Sources and compiled notes separately, canonicalizes the source citation, and updates the source `ingested_into` backlink. |
| Source preservation | Ahead | Pass | Basic Memory importers cover more formats. | Exomem `adopt --mode copy-as-sources` preserves the original file, records `imported_from`, SHA-256, and byte count. |
| Evidence / provenance | Ahead | Pass | Basic Memory is primarily note/knowledge-graph oriented. | Exomem has an explicit Evidence tree plus `provenance_report` over durable `<!-- key:value -->` markers. The marker workflow is still hidden. |
| Schema inference / validation | Missing | Not measured | Basic Memory publicly exposes `schema_infer`, `schema_validate`, and `schema_diff`. | Exomem validates its own page types but has no comparable user-facing schema inference/validation flow. |
| Graph / context building | Behind | Pass | Basic Memory exposes `build_context` and `canvas` as clearer user/assistant workflows. | Exomem can do inbound links and `find(pack=true)`, and `suggest_links` returns a list, but the product flow is fragmented and less legible. |
| Review / stale / contradiction workflow | Ahead | Pass | Basic Memory has recent activity and graph navigation, but less explicit epistemic review. | Exomem `audit`, `attention`, and `propose_compilation` surface unprocessed source work and scaffold compilation. |
| Assistant onboarding | Comparable | Pass | Basic Memory has broader packaged plugins, skills, and public docs across clients. | Exomem `bootstrap` exposes front-door actions and `demo --json` proves doctor/find/get/audit against a sample vault. |

Current harness summary: 10 flows; 4 ahead, 2 comparable, 3 behind, 1 missing. Status: 8 pass, 1 partial, 1 not measured.

## Prioritized backlog

1. **Fix pre-init adoption through the public CLI/MCP surface.** `overview` and `adopt --mode scan-only` must be able to scan a raw messy vault before `Knowledge Base/_Schema/SKILL.md` exists. This is the largest product trust gap because it breaks the exact first-run scenario Exomem claims to handle safely.

2. **Collapse onboarding into one obvious first action.** Keep `setup`'s safety, but reduce the user's mental model to: demo, choose vault, scan, initialize. Basic Memory is ahead because its first-run story is easier to explain and it has a cloud escape hatch.

3. **Make graph/context a single product verb.** Exomem has good pieces (`find(pack=true)`, inbound links, suggestions), but Basic Memory's `build_context`/canvas story is easier to use. Add or document one front-door context command that returns the relevant notes, links, and surrounding graph.

4. **Decide whether schema inference belongs in Exomem.** Basic Memory is plainly ahead here. If Exomem wants this capability, expose it as a governed validation/audit flow, not as a Basic Memory clone.

5. **Make evidence/provenance discoverable.** The Evidence tree and `provenance_report` are a real differentiator, but the HTML-comment marker workflow is too implicit. Add assistant guidance and maybe a small `prove`/`trace` wrapper so users do not need to know the comment syntax.

6. **Polish write ergonomics without dropping governance.** `add` + `note --field sources=...` is safer than a plain note write, but heavier. A product-level remember flow should guide the assistant through Source vs Note vs Evidence without requiring the user to name page types.

## Harness coverage notes

- The benchmark intentionally runs real Exomem CLI commands through subprocesses.
- Heavy embedding/media paths are disabled so the harness stays local, fast, and deterministic.
- `schema_inference_validation` is rated `missing` rather than failed because there is no Exomem command to run.
- `messy_vault_adoption` is partial by design until the pre-init CLI resolution bug is fixed.
- The Basic Memory reference detector reads public-facing README/docs text and records observed product promises; it does not execute or copy Basic Memory code.
