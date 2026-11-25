# Proposal

## Why

The product is now Evermatter. Agents and users still see the old name in the plugins, the skills, the MCP server name and its tool prefix, the managed blocks that setup writes, and the settings prefix. This change supersedes design decision D2 of `rename-to-evermatter`, which kept those identifiers as `exomem`: agent-facing identifiers take the product name.

These names are interfaces. Clients store them, and hooks and permission rules match them. The rename must therefore leave every existing install working until the user moves, and a move must lose no save.

## What Changes

- **Release A accepts both names and still emits the old ones.**
  - Setup and doctor recognise both server keys, both managed-block markers and both readiness service names.
- **Release B emits the new names.**
  - The plugins become `evermatter` and `evermatter-cloud` through the marketplace `renames` map.
  - The server key and the serverInfo name become `evermatter`.
  - The skills become `evermatter-*`.
  - `evermatter setup --migrate` moves an existing client configuration to the new key, with a backup.
  - The Cloud plugin moves to the `mcp.evermatter.ai` address in the same release.
- **Settings accept `EVERMATTER_*` in their own release.** Each source (the process environment, a `.env` file, the standalone hooks) maps the new prefix to the internal one before its own precedence and conflict rules apply. An explicit `EXOMEM_*` value is never overridden after the fact.
- The marketplace id stays `exomem`, and its description names Evermatter (the marketplace schema has no display-name field). Claude Code documents no path for renaming a marketplace, so a rename would strand every install.
- Stored identifiers keep the internal name:
  - the `exomem_id` frontmatter key;
  - state and config paths;
  - the Python package.

  Renaming them would rewrite users' notes and state for no visible gain.
- The `exomem://` reference scheme is out of scope here. It needs its own design, because refs are stored inside notes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `exomem-identity`: agent-facing identifiers take the product name, with a compatibility window for the old ones.

## Impact

- `src/exomem/client_config.py`, `install_hook.py`, `_hooks/*`, `doctor.py`, `setup` and `upgrade` commands, `env_compat.py`, `cloud_plugins.py`, `server.py`, the plugin trees under `plugins/`, `.claude-plugin/marketplace.json`, the skill generators, and the docs.
- Users whose own permission rules or hooks name `mcp__exomem__*` keep working until they migrate. Doctor lists what still names the old prefix.
- The Cloud plugin's move may need one new sign-in per Claude Code install. The operator's choice on that is recorded in the design.
