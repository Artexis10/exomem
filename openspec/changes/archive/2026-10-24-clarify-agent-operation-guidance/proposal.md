## Why

The tool catalogue mixes clear task names with broad family names and uneven guidance about effects and required inputs.
Agents and users need to understand the selected action, including what a permission prompt can mean, without learning the storage implementation.

## What Changes

- Give every product command a plain public title owned by the shared command registry.
- Review all 33 product tools for purpose, neighboring choices, input requirements, effects, results, and recovery guidance.
- Explain read, write, and conditional effects beside the relevant selector without creating a second operation registry.
- Align server instructions, bootstrap guidance, skills, hooks, and generated clients around the same concise workflows.
- Retain the six preferred names from `clarify-agent-tool-surface`, compatible calls, operation families, and existing safeguards.
- Keep host permission presentation distinct from server annotations and invocation authorization.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `product-command-surface`: registry-owned public titles and consistent, purpose-scoped operation guidance across client surfaces.

## Impact

The shared command metadata, MCP annotations, tool descriptions, bootstrap teaching, scaffold, hooks, and generated plugins change together.
This change adds no dependency, model, executor, operation, permission bypass, or data migration.
Grouped tools retain conservative annotations; provider acceptance and rendered host behavior require separate evidence.
