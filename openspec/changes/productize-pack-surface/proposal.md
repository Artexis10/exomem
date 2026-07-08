## Why

Knowledge packs exist, but they still read like internal routing config. Fresh
users with empty vaults need a useful first choice, and agents need pack guidance
that translates simple user intent into governed Exomem operations without
requiring users to learn Sources, Evidence, Notes, and Entities first.

## What Changes

- Expand built-in knowledge packs with product metadata: purpose, audience,
  beginner-facing description, agent-facing instructions, default note/entity/block
  types, suggested folders, and suggested workflows.
- Persist selected packs under the governed Knowledge Base layer so setup,
  bootstrap, MCP/REST/CLI, and future personalization share the same pack state.
- Make `exomem setup` offer explicit pack selection for fresh vaults and confirm
  inferred packs for existing vaults.
- Enrich bootstrap and front-door metadata so agents can route save, ask, prove,
  review, update, adopt, and connect through typed tools while speaking product
  language.
- Update knowledge-pack docs and generated capability docs when the public command
  surface changes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `cognition-layer`: knowledge packs become user-selectable product primitives
  with richer metadata and selected-pack persistence.
- `guided-setup`: setup supports explicit fresh-vault pack selection and writes a
  governed selected-pack manifest.
- `agent-bootstrap-contract`: bootstrap exposes available/selected packs and
  agent routing guidance.
- `command-surface`: front-door metadata includes pack-aware product guidance
  while typed tools remain authoritative.

## Impact

Affected code includes `src/exomem/knowledge_packs.py`,
`src/exomem/setup_wizard.py`, `src/exomem/commands.py`, built-in pack JSON files,
and docs. Tests cover pack validation, setup/bootstrap behavior, adoption output,
and MCP schema stability. No new dependencies or server-side reasoning models are
introduced; pack suggestion and selection remain deterministic pure-substrate
metadata.
