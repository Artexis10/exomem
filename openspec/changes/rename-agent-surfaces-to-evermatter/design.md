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
- After the switch releases, a person or agent sees `evermatter` in the plugin, skill, server and tool names and in the documented settings.
- An install made before the switch keeps working with no action, and a move loses no save.

**Non-Goals**
- Renaming stored identifiers: `exomem_id` frontmatter, state and config paths, the Python package, the `exomem://` reference scheme and the Cloud infrastructure names. `rename-stored-identifiers-to-evermatter` moves them, because each one rewrites stored data or live resources.
- Renaming the marketplace id.

## Decisions

### D1. Two releases: accept, then emit

Release A teaches every reader both names and changes nothing a user sees. Release B switches what we emit. A client can then never meet a new name that our own hooks, setup or doctor fail to recognise. The cost is one extra release, and the release train ships every few days.

### D2. One definition per name set

The accepted server names live in `client_config` beside the emitted `SERVER_NAME`. The readiness service names live beside the readiness payload that emits them. Every reader imports these; no reader keeps its own copy. Release B changes the emitted value and leaves the accepted set unchanged.

### D3. Settings take the `EVERMATTER_` prefix; `EXOMEM_` stays an alias

The documented name of every setting becomes `EVERMATTER_X`. `EXOMEM_X` keeps working as an alias through the compatibility window. A promotion that copies one prefix over the other after the fact is wrong: it beats values that our own code forces, that a `.env` file sets or that an isolation recipe exports, and the literal-name conflict checks never see a conflict across the two prefixes.

Each source therefore resolves the two names before its own rules apply:
- The process environment fills `EVERMATTER_X` from `EXOMEM_X` only when `EVERMATTER_X` is unset.
- A `.env` load resolves its keys before the conflict check and the override.
- The standalone hooks resolve the names in their own alias normaliser.

When a source sets both names to different values, the service and every command that writes a credential refuse to start and name the two variables. The hooks log the conflict and use the `EVERMATTER_` value. Writers emit only `EVERMATTER_*`: setup, the installers, the service files, the container images, the Helm chart, cellctl manifests and the remote setup wizard. Scrubs and denylists cover both names. One function defines the pair of names for a setting. The hooks keep a copy, because they cannot import the package, and a test keeps that copy in step.

### D4. Plugins move through the marketplace `renames` map; the marketplace id stays

Claude Code documents a top-level `renames` map in `marketplace.json`. It rewrites `enabledPlugins` and `pluginConfigs` to the new plugin name and shows a one-time notice. A git-hosted marketplace needs one `/plugin install` of the new name. Claude Code documents no path for renaming a marketplace, so the marketplace id stays `exomem`. The marketplace schema has no display-name field, so its description names Evermatter.

### D5. Existing client configurations move only when the user asks

`evermatter setup --migrate` renames the stdio server key that setup wrote, and the managed instruction block, with a backup of each file (once per run, before the first change). It adds the new prefix beside the old one in explicit checkpoint bindings. It never edits rules or hooks that the user wrote. It lists the ones that no `exomem` server answers any more instead, and doctor lists the same ones on every run.

It skips every registration that has a URL, in Claude Code and in Codex. Both clients store the sign-in under the server name, so a rename signs the user out. The command prints each skipped URL and the reason. A new registration has no stored sign-in, so a fresh install writes `evermatter`.

`evermatter setup` and `evermatter install-skill` move a previously installed `exomem` or `exomem-*` skill folder to a backup folder beside the client's `skills` folder. They match the folder by the `name` in its SKILL.md frontmatter and never delete one. A skill rename signs nobody out.

### D6. Sign-in follows the server name

Claude Code stores the OAuth sign-in of an HTTP server under its name, and under the plugin and server names for a plugin's server. Codex keys it by server name. Renaming a plugin, its server key or an HTTP registration therefore signs each user out once. The Cloud plugin's move to the `mcp.evermatter.ai` address has the same cost. The old token stays valid on the server, and the hooks queue captures while a client is signed out, so no save is lost.

Decision (2026-11-25): accept one sign-in per connection. Every connection that signs in moves to the new name in one release, and a notice goes out before it. For the Cloud plugin, that release also moves the address, so a Cloud user signs in once, not twice. This replaces the earlier rule that no connected client signs out, for this rename only.

Release B renamed nothing that holds a sign-in. The local plugin kept the name `exomem` and the server key `exomem`, `marketplace.json` had no `renames` entry, and `setup --migrate` skipped HTTP registrations.

### D7. Release C renames the connections that sign in

Release C1 covers local installs:
- The local plugin becomes `evermatter`, with server key `evermatter`, through a `renames` entry.
- `evermatter setup --migrate` also renames each URL registration. For each one it prints the client's sign-in step: `/mcp` in Claude Code, and `codex mcp login evermatter` in Codex.
- The renamed plugin's SessionStart hook shows a one-time notice naming the sign-in step, once a session finds the server unauthenticated.

Release C2 renames `exomem-cloud` to `evermatter-cloud`, with server key `evermatter`, and moves its address to `https://mcp.evermatter.ai/mcp`. It ships only after the Cloud serves that host and accepts its token audience, and after the service notice has gone out.

## Risks / Trade-offs

- Each connection that signs in needs one new sign-in after Release C (D6).
- A user's own permission rules that name `mcp__exomem__*` stop matching for a server that the migration renamed. Doctor lists them, and the migration never runs unasked. A rule that still has an `exomem` server behind it, such as a kept URL server, matches and is not listed.
- Codex's OAuth keying is undocumented, so Codex Cloud users may need one `codex mcp login` after migrating. The release notes say so.
- Once the plugins rename, `renames` stays append-only, so the old plugin names remain in `marketplace.json` permanently.

## Removal

On or after 2027-11-23, review gateway traffic per server name and setup telemetry, if any. Remove the old names from the accepted sets only with evidence that nothing uses them.
