## Why

Editing an ordinary frontmatter-less template exposed inconsistent write contracts: the normal editor rejects a supported Markdown shape, overwrite validation returns a token under a different name than commit accepts, graph rebuild failures erase their cause and can recommend an inapplicable reconcile, and Windows audit reports every semantic sidecar as unreadable. Canonical Markdown remains authoritative, so these derived and adapter failures need deterministic, honest recovery without weakening governance.

## What Changes

- Let `edit_memory` body, string, batch-string, and section operations edit frontmatter-less Markdown without synthesizing YAML; keep frontmatter- and tag-dependent operations strict.
- Stop reporting a supplied, resolved path as missing when a page has no frontmatter.
- Make tier-2 create-plus-overwrite validation return a commit-ready `draft_token` while retaining the existing `transition_token` response alias and append-only `semantic_transition_token` behavior.
- Preserve the underlying graph rebuild failure in service diagnostics and return remediation that matches the current graph-lineage classification.
- Keep ordinary reconcile automatic and non-destructive; add an explicit `rebuild_graph=true` reconcile option for reviewed quarantine and reconstruction of ambiguous derived graph lineage from canonical Markdown.
- Implement a Windows-safe no-follow semantic-sidecar census, or report a distinct unsupported state when a sidecar cannot be safely bound; never translate platform non-support into corruption.

## Capabilities

### New Capabilities

- `semantic-sidecar-audit`: Cross-platform, no-follow semantic-sidecar census and truthful availability classifications.

### Modified Capabilities

- `command-surface`: Align editor eligibility, overwrite token replay, graph-repair parameters, and remediation fields with the public schema.
- `records`: Make ordinary Records templates editable through the normal editor without frontmatter synthesis.
- `live-index-freshness`: Distinguish automatic recoverable drift from explicit derived-graph lineage reset and preserve actionable rebuild failures.

## Impact

The change affects the shared edit leaf and adapter, tier-2 create/overwrite validation responses, semantic transition serialization, graph rebuild coordination and reconcile, the semantic-sidecar audit binder, MCP/CLI/REST schema fixtures, and focused Windows/Linux regression coverage. Existing tokens and ordinary reconcile calls remain compatible. No canonical Markdown migration, optional model, or automatic destructive repair is introduced.
