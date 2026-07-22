## Why

Exomem 0.29.1 has regressed from millisecond-scale warm operations to corpus-scale foreground work: a live keyword-only query took 55.1 seconds (12.2 seconds in semantic-unit projection and 41.3 seconds in outside-KB widening), while a guarded `observe_memory` validate/commit pair took 29/16 seconds. The same deployment also left the user-facing `exomem` command on a separate lean 0.4.1 environment while the service ran 0.29.1 with media/vision extras, causing misleading BM25-only fallback warnings and proving that install provenance is not one coherent runtime contract.

## What Changes

- Replace foreground full-vault semantic parsing and stat-census validation with an event-maintained, incrementally patched process cache. Startup warms it off the request path; canonical writes, watcher events, and a bounded census fallback keep it current.
- Make semantic-unit recall and outside-KB widening query their maintained derived indexes directly and hydrate only bounded selected/current pages; these optimized warm lanes MUST NOT scan or parse the whole vault.
- Add explicit warm/cold and post-unrelated-change latency gates over realistic 2k/8k corpora, with per-stage diagnostics and scaling bounds that reject O(corpus) foreground behavior.
- Reconcile an existing uv-managed user-facing CLI with the verified managed-service release during upgrade. Add a cheap version/provenance surface and fail upgrade verification when any visible `exomem`/`kb` executable remains stale.
- Preserve the historical `exomem find` command as a compatibility alias for the current bounded `ask` surface.
- Preserve optional embedding/CLIP/media capabilities as default-off/soft-fail for lean installations; when absent, diagnostics identify the intentional install profile without claiming the managed service has the same capability state.

## Capabilities

### New Capabilities

- `foreground-latency-slo`: Defines bounded foreground read/validate/commit latency, realistic scaling gates, and stage-level evidence for regressions.

### Modified Capabilities

- `live-index-freshness`: Extends event-maintained freshness to semantic corpus/unit state and outside-KB lexical state, including restart and external-sync reconciliation.
- `find-recall-efficiency`: Removes corpus walks/parses from the normal semantic-unit and outside-KB query paths while preserving recall and freshness behavior.
- `install-readiness`: Records managed service provenance, reconciles stale uv-tool CLIs during upgrade, and verifies every PATH-visible Exomem command before success.
- `command-surface`: Adds a cheap, stable version/provenance CLI surface that does not import optional ML stacks and remains consistent with install diagnostics.

## Impact

This affects semantic corpus/index state, `find`/`ask_memory`, governed write preflight, watcher behavior, Windows and Unix upgrade scripts, CLI provenance reporting, and CI/benchmark gates. It changes no Markdown source-of-truth rule, MCP mutation schema, semantic language, or pure-substrate boundary. Embedding, CLIP, OCR, ASR, and other models remain optional extras; the change does not duplicate them into the lean CLI environment.
