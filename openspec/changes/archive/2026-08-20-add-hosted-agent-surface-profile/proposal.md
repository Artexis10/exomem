## Why

The shared Hosted MCP facade needs one stable, least-privilege Exomem tool contract for Claude and ChatGPT. The cell currently publishes its complete private control-plane command registry, so consuming that contract directly would expose alpha-inappropriate operations and let the public gateway, bootstrap guidance, and future client packages drift apart.

## What Changes

- Add an immutable `hosted-alpha-agent-v1` profile beside the canonical product command registry.
- Limit that profile to governed text-memory capture, recall, review, and connection operations; exclude transfers, adoption, media processing, maintenance/schema administration, coordination internals, and every Tier-2 operation.
- Add a deterministic MCP-ready registry contract and capability fingerprint for the profile without changing the existing private cell contract or its digest.
- Add authenticated private agent-contract and agent-command routes that enforce the profile and bind its active descriptor inside the cell.
- Make `bootstrap` advertise only commands in the active Hosted agent profile when invoked through that surface.
- Add drift and compatibility tests proving the profile is registry-derived, deterministic, internally coherent, and additive to existing Hosted behavior.

## Capabilities

### New Capabilities

- `hosted-agent-surface`: Defines the versioned private-alpha agent tool profile, its deterministic contract, its bootstrap parity, and its compatibility boundary with the full private cell contract.

### Modified Capabilities

None.

## Impact

- Affected code: the Exomem product profile registry, Hosted gateway contract generation, authenticated private Hosted routing, active capability metadata, and focused contract/bootstrap tests.
- Consumers: the future Substrate shared MCP facade can consume the named profile instead of maintaining its own allowlist.
- Compatibility: existing private routes, release manifests, control-plane fixtures, full MCP/REST/CLI surfaces, and their digests remain unchanged by default.
- Explicitly out of scope: OAuth, public MCP transport, Substrate UI, provider infrastructure, deployment, client skill/plugin packaging, file transfer, media, adoption, and performance work.
