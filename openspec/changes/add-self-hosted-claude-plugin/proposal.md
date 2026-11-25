## Why

A self-hosted Evermatter user on claude.ai has no one-upload install. The hosted plugin's connection points at the hosted service, and the self-hosted edition carries skills and hooks but no connection, so the user must also add a custom connector by hand. claude.ai cannot ask a plugin for a server address: Chat ignores an MCP server whose URL references `${user_config.*}`, and Cowork does not prompt for one. Each self-hosted server therefore needs a plugin built for its own address, without a maintainer in the loop.

## What Changes

- A self-hosted server with a public origin (`EXOMEM_BASE_URL`) serves `GET /claude-plugin.zip`: the release's connector edition, renamed for that server, with a `.mcp.json` that connects to the server's `/mcp`.
- The wheel carries the generated connector edition archive, so an installed server builds the plugin from release bytes.
- `exomem setup --remote` ends by pointing at the download, upload and connect steps, and keeps the custom-connector path as the alternative.

## Capabilities

### New Capabilities

- `self-hosted-claude-plugin`: a self-hosted server serves a one-upload Claude plugin for its own public origin.

## Impact

- New `src/exomem/self_hosted_plugin.py`; a route in `src/exomem/server_assets.py`, registered in `src/exomem/server.py`.
- `pyproject.toml` force-includes `plugins/cloud/generated/claude-connector-hooks.zip` in the wheel and lists it in the sdist.
- `src/exomem/remote_setup_wizard.py` closing text.
- No change to the generated editions, their plugin names or the hosted plugin.
