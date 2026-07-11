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

Exomem is stronger where the product needs governance: stable object identity,
source/evidence separation, provenance, supersession, review queues, optional
hash-guarded schema contracts, and safe copied-source preservation. Basic Memory
is stronger where the product needs low-friction adoption and breadth:
cloud/no-install paths, sync, importers, multi-project UX, canvas workflows, and
the packaged assistant ecosystem. The earlier technical schema and unified
context gaps are now closed. The Review Studio also closes the largest human
product-surface gap: governed review, activation, provenance, explicit
decisions, and recorded belief evolution now form one packaged browser loop.
Basic Memory still leads on generic editor breadth, cloud onboarding, sync,
importers, and collaboration; Exomem should not chase that whole surface before
measuring Studio usage.

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
| Graph / context building | Comparable | Pass | Basic Memory still exposes a broader canvas workflow. | Exomem now has one bounded `connect_memory(operation="context")` response over stored documents, semantic blocks, typed graph edges, provenance, evidence, supersession, history, unresolved targets, and explicit truncation. |
| Review / stale / contradiction workflow | Ahead | Pass | Basic Memory has recent activity and graph navigation, but less explicit epistemic review. | Exomem `review_memory` surfaces unprocessed source work, audit findings, attention queues, provenance, and compilation scaffolds. |
| Human review control plane | Ahead | Acceptance pass | Basic Memory has broader note/editor/canvas UX; its public surface does not expose the same governed daily review and recorded-evolution loop. | Packaged `/studio/` preserves server ranking, separates opt-in Activation, composes bounded context, fingerprint-guards triage, and keeps relation/compile/supersession proposals read-only until a separate existing-command confirmation. |
| Assistant onboarding | Comparable | Pass | Basic Memory has broader packaged plugins, skills, and public docs across clients. | Exomem `bootstrap` exposes front-door actions and product commands; `demo --json` proves doctor/retrieval/review against a sample vault. |

Current harness summary: 10 flows; 4 ahead, 5 comparable, 1 behind, 0 missing.
Status: 10 pass.

Studio acceptance is additive to that 10-flow harness: the wheel/sdist contains
seven offline assets (about 45 KiB uncompressed; complete wheel about 708 KiB),
and a clean wheel install passed shell, REST-auth rejection, create, Activation
lookup, bounded `review_item_context`, and fingerprint-guarded triage. Focused
Studio/context/product-surface tests passed. The lean suite reached 1,885 passed
with one unrelated order-sensitive schema test that passed immediately in
isolation. Repository-wide Ruff still reports its existing uncurated baseline;
every changed Python file is clean.

## Prioritized backlog

1. **Keep first-run adoption polished across surfaces.** The CLI now lets `browse_memory(mode="overview")` and `adopt_vault(mode="scan-only")` scan a raw messy vault before `Knowledge Base/_Schema/SKILL.md` exists. Keep MCP and REST aligned with that contract, and make the safe next action obvious.

2. **Collapse onboarding into one obvious first action.** Keep `setup`'s safety, but reduce the user's mental model to: demo, choose vault, scan, initialize. Basic Memory is ahead because its first-run story is easier to explain and it has a cloud escape hatch.

3. **Measure the Review Studio loop before adding breadth.** Use only
   privacy-preserving local product counters: Inbox opens, inspected items,
   proposal starts, confirmations, cancellations, and changed-signal refreshes.
   The next product decision should follow observed friction, not an assumption
   that Exomem needs a generic editor or canvas.

4. **Polish write ergonomics without dropping governance.** `capture_source` +
   `remember --field sources=...` is safer than a plain note write, but heavier.
   The product remember flow should guide the assistant through Source vs Note vs
   Evidence without requiring the user to name page types.

5. **Keep the heavy proof lanes operational.** Lean stdio/HTTP product E2E now
   runs on every pull request; real embeddings/reranking remain in the model job,
   and OCR/PDF/ASR/CLIP/video run scheduled or on demand. Treat failures in those
   configured lanes as product regressions, not optional noise.

## Review Studio limitations and next decision

- Single-user only; no cloud sync, teams, CRDTs, public sharing, or generic note
  editor.
- Session-scoped bearer auth is secure but less smooth than a dedicated local
  session exchange. Cloudflare Access remains the better remote personal path.
- Browser acceptance is opt-in and adds neither Playwright nor Node to runtime
  dependencies.
- Inbox and Activation can share a stable target ref while carrying different
  reasons; fingerprint-aware resolution preserves the selected mode and refuses
  stale writes.
- The next measured decision is whether users stall at authentication, context
  comprehension, or governed confirmation. Do not build editor/canvas breadth
  until that evidence exists.

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
