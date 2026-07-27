## Why

The hosted plugin's compatibility descriptor embeds the Exomem release, so its
`compatibility_sha256` changes on every version bump — including patch releases
that do not touch the hosted contract at all.

Two costs follow. Every release invalidates the committed generated artifacts,
which is why release CI broke on 0.34.0 and needed the regeneration job added in
#342. Worse, and not yet felt: `compatibility_sha256` is the published
descriptor's identity, and a live promotion record whose hash no longer matches
the repository raises `live promotion record has stale package bindings`. A
patch release therefore forces a re-promotion of a plugin whose contract is
byte-for-byte unchanged.

Nothing consumes the coupling. `source_release` is read nowhere outside
`hosted_plugins.py`, and plugin promotion binds identity through
`compatibility_sha256`, `schema_contract_sha256`, `command_surface_sha256`,
`plugin_version`, `profile` and the two package digests — never the release.

## What Changes

- Remove `source_release` from the hosted plugin definition and its schema.
- Exclude the Exomem release from the compatibility descriptor's hashed identity
  so the descriptor identifies the contract shape, not the build that emitted it.
- Keep the runtime agent gateway contract reporting `exomem_release` unchanged.
- Drop the equality guard that made `hosted-plugin.py regenerate` refuse to run
  until the definition was resynced — the condition it guarded ceases to exist.
- Retire `scripts/sync_hosted_release.py` and the release-branch resync step it
  serves, keeping the regeneration job only where a real contract change needs it.
- Add regression coverage proving a version bump alone leaves
  `compatibility_sha256` and the committed artifacts unchanged.

## Capabilities

### New Capabilities

- `hosted-plugin-identity`: The published hosted plugin descriptor is identified
  by its contract surface, and an Exomem release that does not change that
  surface does not change the descriptor or invalidate a live promotion.

### Modified Capabilities

None.

## Impact

Affected areas are `src/exomem/hosted_plugins.py`, `plugins/hosted/definition.json`,
the generated descriptors under `plugins/hosted/generated/`, hosted plugin tests,
`scripts/sync_hosted_release.py`, and the `sync-hosted-artifacts` release job.

This changes `compatibility_sha256` once. Any live promotion record must be
re-promoted on the release carrying this change, after which release-driven
churn stops. No MCP tool schema, OAuth, vault format, or stdio behavior changes.
