# Product gap matrix: Exomem vs Basic Memory

Product-flow baseline measured 2026-07-09; direct graph comparison added
2026-07-11.

This is a product benchmark, not an internals comparison. The product-flow
baseline reads Basic Memory's public README/docs/tool names; the graph row adds a
direct run through its public `build_context` MCP tool over native Markdown. No
Basic Memory implementation or benchmark code is copied.

Run locally:

```powershell
uv run python scripts/product_flow_benchmark.py
uv run python scripts/product_flow_benchmark.py --json
uv run python scripts/product_flow_benchmark.py --flow fresh_setup --flow search_recall
```

The harness creates temporary vaults under `.pytest-tmp/product-flow-benchmark/` by default and removes them unless `--keep-tmp` is set.

## Verdict

Exomem is stronger where the product needs governance: stable object identity,
source/evidence separation, provenance, supersession, review queues, optional
hash-guarded schema contracts, and safe copied-source preservation. Basic Memory
is stronger where the product needs low-friction adoption and breadth:
cloud/no-install paths, sync, importers, multi-project UX, canvas workflows, and
the packaged assistant ecosystem. The earlier technical schema and unified
context gaps are now closed. Exomem's graph is now directly benchmarked as ahead
on graph-dependent tasks; Basic Memory's remaining lead is primarily product
surface, not knowledge-substrate depth. See
[the graph-value comparison](comparison-basic-memory-graph.md).

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
| Schema inference / validation | Comparable | Pass | Basic Memory publicly exposes `schema_infer`, `schema_validate`, and `schema_diff`. | Exomem's `schema_memory` now infers from corpus frequencies, saves optional contracts with hash-guarded overwrite, validates with strict CI status, and diffs corpus or contract drift. |
| Graph / context building | Ahead | Pass | Basic Memory still exposes a broader canvas workflow. | The direct graph-value benchmark matches Basic Memory on one-hop, multi-hop, and relation typing, then wins direction, distractor precision, traversal lenses, provenance, lifecycle, and semantic-block precision. |
| Review / stale / contradiction workflow | Ahead | Pass | Basic Memory has recent activity and graph navigation, but less explicit epistemic review. | Exomem `review_memory` surfaces unprocessed source work, audit findings, attention queues, provenance, and compilation scaffolds. |
| Assistant onboarding | Comparable | Pass | Basic Memory has broader packaged plugins, skills, and public docs across clients. | Exomem `bootstrap` exposes front-door actions and product commands; `demo --json` proves doctor/retrieval/review against a sample vault. |

Current harness summary: 10 flows; 5 ahead, 4 comparable, 1 behind, 0 missing.
Status: 10 pass.

## Prioritized backlog

1. **Turn graph superiority into habitual product value.** Use
   `review_memory(mode="activation")` to measure and rank relationship debt in
   existing vaults, make propose/review/apply easy, and show graph paths,
   provenance anchors, and active/superseded state when they improve an answer.

2. **Keep first-run adoption polished across surfaces.** The CLI now lets
   `browse_memory(mode="overview")` and `adopt_vault(mode="scan-only")` scan a
   raw messy vault before initialization. Keep MCP and REST aligned and reduce
   the first-run mental model to: demo, choose vault, scan, initialize.

3. **Make evidence/provenance discoverable.** The Evidence tree and
   `review_memory(mode="provenance")` are a real differentiator, but the
   HTML-comment marker workflow is too implicit. Add assistant guidance so users
   do not need to know the comment syntax.

4. **Polish write ergonomics without dropping governance.** `capture_source` +
   `remember --field sources=...` is safer than a plain note write, but heavier.
   The product remember flow should guide the assistant through Source vs Note vs
   Evidence without requiring the user to name page types.

5. **Grow schema from observed graph pressure, not in advance.** Add relation
   vocabulary or constraints only when activation and user-task evidence shows a
   repeated semantic collision that changes traversal or review outcomes. Keep
   the graph benchmark and heavy proof lanes operational as the promotion gate.

## Harness coverage notes

- The benchmark intentionally runs real Exomem CLI commands through subprocesses.
- Heavy embedding/media paths are disabled so the harness stays local, fast, and deterministic.
- `schema_inference_validation` seeds five consistent pages, then exercises
  infer/save, strict validate, and corpus diff through the CLI product surface.
- `graph_context_building` reconciles derived graph state and checks the unified
  bounded context envelope in addition to suggestions, inbound links, and deep
  recall.
- `messy_vault_adoption` covers the fixed pre-init scan path and confirms scan-only leaves originals untouched.
- The Basic Memory reference detector reads public-facing README/docs text and records observed product promises; it does not execute or copy Basic Memory code.
