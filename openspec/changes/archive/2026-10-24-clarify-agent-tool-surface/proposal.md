## Why

Several public tool names obscure their jobs. Agents must distinguish context recall, evidence search, note preparation, editing, and supersession before choosing a tool.

## What Changes

- Publish six clearer names while retaining previous callable names across MCP, REST, and CLI.
- Keep each internal command identity, operation boundary, parameter, response, and safeguard unchanged.
- Update tool guidance and generated clients to recommend the preferred names and avoid redundant calls.
- Preserve the current tool count and schema budgets. Add no generic executor or server-side model.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `command-surface`: preferred public names share stable execution identities and backward-compatible dispatch across generated surfaces.

## Impact

The command registry, MCP middleware and discovery, REST routes and OpenAPI, CLI parsing, capability metadata, hooks, scaffold, and generated plugins change together.
Stored knowledge and historical frozen contracts require no migration.
