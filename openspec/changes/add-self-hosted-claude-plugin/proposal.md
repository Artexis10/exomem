## Why

A self-hosted Evermatter user on claude.ai has no one-upload install. The hosted plugin's connection points at the hosted service, and the self-hosted edition carries skills and hooks but no connection, so the user must also add a custom connector by hand. claude.ai cannot ask a plugin for a server address: Chat ignores an MCP server whose URL references `${user_config.*}`, and Cowork does not prompt for one. Each self-hosted server therefore needs a plugin built for its own address, without a maintainer in the loop.

## What Changes

- The generator builds a fourth Claude edition, `claude-server`: the hooked edition's hooks and skills with a bundled server at a reserved placeholder origin. It ships inside the Python package at `src/exomem/_plugins/claude-server.zip` and is not listed in the marketplace.
- A self-hosted server with a public origin (`EXOMEM_BASE_URL`) serves `GET /claude-plugin.zip`: that edition with the placeholder replaced by the server's origin and the plugin named after its host.
- `exomem setup --remote` and the deployment docs point at the download first and keep the custom connector as the alternative.

## Capabilities

### New Capabilities

- `self-hosted-claude-plugin`: a self-hosted server serves a one-upload Claude plugin for its own public origin.

## Impact

- `src/exomem/cloud_plugins.py` and `plugins/cloud/definition.json` gain the `claude-server` edition; `src/exomem/_plugins/claude-server.zip` is generated and checked by `scripts/cloud-plugin.py check`.
- New `src/exomem/self_hosted_plugin.py`; a route in `src/exomem/server_assets.py`, registered in `src/exomem/server.py`.
- `src/exomem/remote_setup_wizard.py`, `docs/remote-quickstart.md`, `docs/deployment.md`.
- No change to the existing editions, their plugin names, the hosted plugin, the package inputs or the container build.
