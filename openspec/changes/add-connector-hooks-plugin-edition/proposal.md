## Why

The hooked Claude edition bundles an MCP server bound to Evermatter Cloud. An account that already connects Evermatter in another way, such as a self-hosted server added as a connector, cannot use it: installing it adds a second, unauthenticated server for the wrong account. Claude Code cloud sessions and project threads load plugins only from the account, not from the repository, so these accounts have no way to get the lifecycle hooks.

## What Changes

- Add a third Claude edition, `generated/claude-connector-hooks`, built from the same definition. It carries the hooked edition's hooks and skills byte for byte and bundles no MCP server.
- List it in the repository marketplace under its own plugin name, `evermatter-self-hosted`, with the display name "Evermatter (self-hosted)".
- Its readme tells the user to connect Evermatter first and to install the full plugin instead when they have not.
- The hooked edition shows as "Evermatter", and both marketplace editions open their descriptions with the account they serve.
- Claude manifests carry a short summary, and the build refuses a description over 500 characters, which claude.ai's plugin upload rejects.

## Capabilities

### Modified Capabilities

- `cloud-plugin-distribution`: adds the connector edition beside the directory and hooked editions.

## Impact

- `src/exomem/cloud_plugins.py`, `plugins/cloud/definition.json`, `plugins/cloud/generated/`, `.claude-plugin/marketplace.json`, `docs/cloud-plugin-distribution.md`.
- No change to the hooks, the directory edition, the hooked edition or the OpenAI package.
