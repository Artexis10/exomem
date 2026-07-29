## Why

Exomem Hosted needs to feel like a product, not an MCP integration project: an invited user should install one client-native plugin, sign into Exomem, and get governed long-term memory in a fresh conversation without URLs, local setup, custom instructions, skill uploads, or Exomem-specific prompting. The product now has a least-privilege Hosted agent profile, so the missing Exomem-side contract is a reproducible, platform-tested package that binds that profile to Claude and OpenAI clients.

## What Changes

- Add one canonical Hosted plugin definition that renders installable Claude and OpenAI packages for the versioned shared Exomem Hosted MCP resource, including OpenAI's `.app.json` registered-app mapping.
- Bundle a Hosted-safe core skill and compatible workflow skills that teach automatic recall, capture, continuation, and governance without requiring explicit "use Exomem" prompts.
- Make every bundled skill declare its required Exomem tools and fail packaging when a dependency is absent from `hosted-alpha-agent-v1`.
- Bind each package to the Hosted endpoint, profile identifier, command-surface fingerprint, full contract digest, skill-content hash, and plugin version without embedding tenant identifiers, credentials, vault paths, or local runtime configuration.
- Add deterministic package generation, static safety/drift gates, and per-platform pending-to-live promotion records.
- Require real installation plus fresh-client, content-bearing recall and durable capture/recall smoke tests before a Claude or OpenAI artifact is marked ready for distribution.
- Keep the existing local Claude Code plugin and all local Exomem behavior unchanged. Hosted packaging is additive, does not run a server-side reasoning model, and is inactive unless a user installs a Hosted package.

## Capabilities

### New Capabilities

- `hosted-client-plugins`: Defines canonical Hosted plugin packaging, Hosted-safe skill projection, deterministic identity and drift checks, platform promotion, and the no-configuration fresh-chat acceptance contract.

### Modified Capabilities

None.

## Impact

- Affected areas: plugin source and generators, Hosted-safe skill projections, package metadata/assets, release and promotion records, contract tests, and live client smoke tooling.
- External dependency: the paired Substrate change `add-exomem-hosted-mcp-oauth` supplies the public MCP endpoint, Exomem-owned OAuth lifecycle, invite admission, tenant provisioning, and pinned `hosted-alpha-agent-v1` routing.
- Compatibility: local stdio/MCP installs, the current Claude Code plugin, personal connectors, and the full private Hosted control-plane surface remain unchanged.
- Distribution starts private or unlisted for the friends cohort; public directory submission is a later promotion decision, not a requirement for this change.
