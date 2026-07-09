## Why

Exomem has the right governed-knowledge architecture, but the public command
surface still exposes too many internal primitives and forces agents to plan in
implementation terms. The next product step is a capability-complete command
surface that is as powerful as the current MCP/REST/CLI layer while using
clearer user-facing concepts.

## What Changes

- Add a first-class product command layer shared by MCP, REST, CLI, OpenAPI, and
  docs.
- **BREAKING**: change the default MCP tool set from canonical primitive names to
  product commands that cover the full system capability.
- Keep canonical leaves such as `find`, `note`, `add`, `preserve`, `audit`, and
  `reconcile` as shared implementation functions; do not duplicate governance,
  validation, vault checks, or write rules.
- Collapse common multi-step workflows into fewer product operations where that
  reduces tool calls without hiding important safety choices.
- Preserve full MCP capability: every governed capability reachable from REST/CLI
  must have a product-command route in MCP unless it is terminal-local setup/admin.
- Keep optional/heavy behavior default-off and soft-failing. Retrieval rerank,
  packed context, graph enrichment, embeddings rebuilds, media/model extraction,
  and model-backed relation suggestions remain explicit options.
- Update MCP schema fixtures intentionally, replacing the byte-identical
  old-tool baseline with a product-surface baseline and coverage tests.

## Capabilities

### New Capabilities

- `product-command-surface`: Capability-complete product commands that present
  Exomem in user/agent language while routing through canonical implementation
  leaves.

### Modified Capabilities

- `command-surface`: The single-surface contract changes from primitive registry
  generation to product command generation over shared canonical leaves.
- `agent-bootstrap-contract`: Bootstrap must teach the product command surface
  first and describe canonical leaves as implementation/advanced concepts only
  where needed.

## Impact

- Affected code: `src/exomem/commands.py`, `src/exomem/command_surface.py`,
  `src/exomem/server.py`, `src/exomem/server_rest.py`, `src/exomem/__main__.py`,
  tool schema fixtures, OpenAPI generation, CLI help, and scaffold docs.
- Affected public API: MCP tool names and REST/CLI command names for product
  operations intentionally change. Canonical leaf functions remain in-process
  implementation contracts.
- Affected tests: MCP schema fidelity, REST registry, CLI core ops, bootstrap,
  tool annotations, docs/capabilities generation, scaffold leak tests, and
  focused product-flow tests.
- No new runtime dependency and no server-side reasoning LLM.
