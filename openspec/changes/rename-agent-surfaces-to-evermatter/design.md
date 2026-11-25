# Design

## Context

`rename-to-evermatter` renamed every external surface and kept the identifiers that clients store: the MCP server key `exomem` and the plugin and skill names (its D2). This change supersedes that decision: agent-facing identifiers take the product name.

These identifiers are interfaces. A client stores the server key it was given and builds tool names from it:
- a user-scope server's tools are `mcp__<server>__<tool>`;
- a plugin's tools are `mcp__plugin_<plugin>_<server>__<tool>`.

Hooks and users' permission rules match those names. Claude Code keys stored MCP OAuth by server name plus a hash of the URL. This was checked in Claude Code 2.1.331 on 2026-11-24. The design follows "Writing effective tools for agents" on namespacing: a prefix changes only with a migration path.

The code is mostly ready to accept both names:
- the capture hook's server pattern already matches `evermatter`;
- the retrieve hook compares a tool name against the server name the host reports;
- explicit checkpoint bindings accept up to eight prefixes.

The single-name sites are the following:
- the emitters: `client_config.SERVER_NAME`, the plugin manifest generator and the Cloud plugin generator;
- setup's lookup and removal of the key `exomem`;
- the managed instruction-block markers;
- doctor's readiness check of `service == "exomem"`;
- one auth harness script.

## Goals / Non-Goals

**Goals**
- After the switch release, a person or agent sees `evermatter` in the plugin, skill, server and tool names and in the documented settings.
- An install made before the switch keeps working with no action, and a move loses no save.

**Non-Goals**
- Renaming stored identifiers: `exomem_id` frontmatter, state and config paths, the Python package, and the `exomem://` reference scheme. Refs live inside users' notes, so a scheme change needs its own design.
- Renaming the marketplace id.

## Decisions

### D1. Two releases: accept, then emit

Release A teaches every reader both names and changes nothing a user sees. Release B switches what we emit. A client can then never meet a new name that our own hooks, setup or doctor fail to recognise. The cost is one extra release, and the release train ships every few days.

### D2. One definition per name set

The accepted server names live in `client_config` beside the emitted `SERVER_NAME`. The readiness service names live beside the readiness payload that emits them. Every reader imports these; no reader keeps its own copy. Release B changes the emitted value and leaves the accepted set unchanged.

### D3. Settings accept `EVERMATTER_*` per source, in a release of their own

Settings are not an identifier that clients store, so they need not ship with Release A. A promotion that copies `EVERMATTER_X` over `EXOMEM_X` after the fact is wrong. It would beat every value our own code forces, a `.env` file sets or an isolation recipe exports. The literal-name conflict checks would also never see a conflict across the two prefixes.

Each source therefore maps the new prefix to the internal name before its own rules apply:
- the process environment fills `EXOMEM_X` from `EVERMATTER_X` only when `EXOMEM_X` is unset, and logs a conflict;
- a `.env` load maps its keys before the conflict check and the override;
- the standalone hooks map the prefix in their own alias normaliser.

Setup's `.env` route lookup and the early resource preload read through the same mapping. One function defines the mapping, and the hooks keep a copy that a test keeps in step, because they cannot import the package.

### D4. Plugins move through the marketplace `renames` map; the marketplace id stays

Claude Code documents a top-level `renames` map in `marketplace.json`. It rewrites `enabledPlugins` and `pluginConfigs` to the new plugin name and shows a one-time notice. A git-hosted marketplace needs one `/plugin install` of the new name. Claude Code documents no path for renaming a marketplace, so the marketplace id stays `exomem`. The marketplace schema has no display-name field, so its description names Evermatter.

### D5. Existing client configurations move only when the user asks

`evermatter setup --migrate` renames the stdio server key that setup wrote, and the managed instruction block, with a backup of each file (once per run, before the first change). It adds the new prefix beside the old one in explicit checkpoint bindings. It never edits rules or hooks that the user wrote. It lists the ones that no `exomem` server answers any more instead, and doctor lists the same ones on every run.

It skips every registration that has a URL, in Claude Code and in Codex. Both clients store the sign-in under the server name, so a rename signs the user out. The command prints each skipped URL and the reason. A new registration has no stored sign-in, so a fresh install writes `evermatter`.

`evermatter setup` and `evermatter install-skill` move a previously installed `exomem` or `exomem-*` skill folder to a backup folder beside the client's `skills` folder. They match the folder by the `name` in its SKILL.md frontmatter and never delete one. A skill rename signs nobody out.

### D6. Sign-in follows the server name

Claude Code stores the OAuth sign-in of an HTTP server under its name, and under the plugin and server names for a plugin's server. Codex keys it by server name. Renaming a plugin, its server key or an HTTP registration therefore signs each user out once. The Cloud plugin's move to the `mcp.evermatter.ai` address has the same cost. The old token stays valid on the server, and the hooks queue captures while a client is signed out, so no save is lost.

This choice belongs to the operator, because it breaks the 2026-11-24 rule that no connected client signs out. One option renames every HTTP registration and both plugins together with the address move, and tells users to sign in once. The other keeps the `exomem` and `exomem-cloud` plugin names and server keys, and leaves that name in their tool names. Release A does not depend on the choice.

Until the operator rules, Release B renames nothing that holds a sign-in. The local plugin keeps the name `exomem` and the server key `exomem`, and `marketplace.json` has no `renames` entry. `setup --migrate` skips HTTP registrations. The plugins' skills are renamed.

## Risks / Trade-offs

- A user's own permission rules that name `mcp__exomem__*` stop matching for a server that the migration renamed. Doctor lists them, and the migration never runs unasked. A rule that still has an `exomem` server behind it, such as a kept URL server, matches and is not listed.
- Codex's OAuth keying is undocumented, so Codex Cloud users may need one `codex mcp login` after migrating. The release notes say so.
- Once the plugins rename, `renames` stays append-only, so the old plugin names remain in `marketplace.json` permanently.

## Removal

On or after 2027-11-23, review gateway traffic per server name and setup telemetry, if any. Remove the old names from the accepted sets only with evidence that nothing uses them.
