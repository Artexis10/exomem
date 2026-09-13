## Why

Agents can read four prominence levels but cannot persist a user's choice through the MCP/plugin surface. A self-hosted HTTP cell consequently reports the Balanced fallback even when the user requests another level, and the legacy machine-wide CLI setting can affect unrelated vaults.

## What Changes

- Add a canonical configuration operation to inspect and set the existing Off, Light, Balanced and Maximal levels through MCP, REST and CLI.
- Persist the choice for the addressed vault and authenticated identity, and read it on subsequent requests without restarting the service or modifying another user's or vault's settings.
- Report the effective level, its source and any operator override honestly; a shadowed setting must not be presented as an applied change.
- Use request client identity for hookless defaults when no explicit preference exists, and teach the configuration route in the bootstrap contract.
- Preserve the existing behavioural contracts, compute modes, governance and delegation ceilings.

## Capabilities

### New Capabilities

- `agent-prominence-control`: Principal- and vault-scoped prominence selection, authorized configuration changes, precedence, persistence and consistent projection for clients presenting the same identity.

### Modified Capabilities

None. The new capability extends the existing prominence implementation without changing the four levels' behavioural meanings.

## Impact

Prominence resolution and storage; canonical command registration and authorization; MCP/REST/CLI adapters; bootstrap, workflow and envelope projections; public client guidance and generated tool metadata. Standalone hooks keep their existing environment/machine configuration because they have no authenticated vault binding. No identity federation, models, heavy dependencies or background workers are added.
