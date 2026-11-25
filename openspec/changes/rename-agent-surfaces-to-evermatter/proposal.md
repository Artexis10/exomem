# Proposal

## Why

The product is now Evermatter. Agents and users still see the old name in the plugins, the skills, the MCP server name and its tool prefix, the managed blocks that setup writes, and the settings prefix. This change supersedes design decision D2 of `rename-to-evermatter`, which kept those identifiers as `exomem`: agent-facing identifiers take the product name.

These names are interfaces. Clients store them, and hooks and permission rules match them. The rename must therefore leave every existing install working until the user moves, and a move must lose no save.

## What Changes

- **Release A accepts both names and still emits the old ones.**
  - Setup and doctor recognise both server keys, both managed-block markers and both readiness service names.
- **Release B emits the new names for everything that holds no sign-in.**
  - The server key of a new registration and the serverInfo name become `evermatter`.
  - The skills become `evermatter-*`.
  - `evermatter setup --migrate` moves an existing stdio client registration to the new key, with a backup.
- **Release C renames the connections that sign in, at the cost of one sign-in each (D6, D7).**
  - The local plugin becomes `evermatter` through the marketplace `renames` map.
  - `setup --migrate` renames HTTP registrations and prints the sign-in step.
  - The Cloud plugin becomes `evermatter-cloud` at the `mcp.evermatter.ai` address, after the Cloud serves that host and a service notice has gone out.
- **Settings take the `EVERMATTER_` prefix in their own release (D3).** `EXOMEM_*` stays a working alias that fills only unset values, and a conflict between the two is refused.
- The marketplace id stays `exomem`, and its description names Evermatter (the marketplace schema has no display-name field). Claude Code documents no path for renaming a marketplace, so a rename would strand every install.
- Stored identifiers move in `rename-stored-identifiers-to-evermatter`: the `exomem_id` frontmatter key, the `exomem://` reference scheme, state and config paths, the Python package and the Cloud infrastructure names.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `exomem-identity`: agent-facing identifiers take the product name, with a compatibility window for the old ones.

## Impact

- `src/exomem/client_config.py`, `install_hook.py`, `_hooks/*`, `doctor.py`, `setup` and `upgrade` commands, `env_compat.py`, `cloud_plugins.py`, `server.py`, the plugin trees under `plugins/`, `.claude-plugin/marketplace.json`, the skill generators, and the docs.
- Users whose own permission rules or hooks name `mcp__exomem__*` keep working until they migrate. Doctor lists the rules and hooks that name the old prefix once no `exomem` server answers them.
- Each connection that signs in needs one new sign-in after Release C. The design records that decision.
