# Tasks

## 1. Release A: accept both names

- [x] 1.1 Define the accepted server names beside `client_config.SERVER_NAME`. Make the Codex lookup, the Codex block merge and setup's Claude registration lookup and removal recognise either name.
- [x] 1.2 Recognise the `evermatter` managed instruction-block markers beside the `exomem` ones. Keep writing the old markers.
- [x] 1.3 Accept either readiness service name in doctor, from one definition beside the readiness payload.
- [x] 1.4 Accept either server name in the Codex auth session harness.

## 2. Settings prefix (D3)

- [x] 2.1 Settings release S1: rename `EVERMATTER_X`, then `KB_MCP_X`, into `EXOMEM_X` when it is unset, and remove the documented name from the process environment. Do this per source: the process environment, the `.env` loads before their conflict check, the standalone hooks' alias normalisers, setup's `.env` route lookup and the early resource preload. Refuse a conflict wherever a placement setting is read or a credential is written. Skip the hook's nudge or checkpoint on a conflict in a placement setting or the hook's home, and let `EXOMEM_X` win any other. Teach the readers outside the package both names: the Cloud gateway's environment check in the control-plane repository, the gateway image's memory health runner (its reader ships in substrate #352), cellctl's own settings, the chart's scheduler runtime and the service scripts' shell and PowerShell readers (`_service-common`, install, upgrade, restart, deploy).
- [ ] 2.2 Settings release S2, at least one release after S1: emit only `EVERMATTER_*` from setup, the installers (shell and PowerShell), the service files, the container images, the Helm chart and the remote setup wizard. Make cellctl choose the names per cell from its target image. Switch a chart component's names only after its reader accepts both in production. Cover every name in each scrub and denylist. S2 pins a gateway image that contains substrate #352, and it switches the image's `ENV EXOMEM_GATEWAY_PORT` no later than the chart.

## 3. Release B: emit the new names

- [x] 3.1 Operator choice on D6 (sign-in cost of renaming a plugin or an HTTP registration, Cloud plugin included) recorded in the design.
- [x] 3.2 Emit the server key (for a new registration) and the serverInfo name `evermatter`. Rename the skills to `evermatter-*` in the workflow-skill index and the core skill. Regenerate the plugin trees.
- [x] 3.3 Release C1: rename the local plugin to `evermatter` with a `renames` entry in `marketplace.json`, and run `claude plugin validate .`. Make `setup --migrate` rename URL registrations and print each client's sign-in step. Show the one-time sign-in notice from the plugin's SessionStart hook.
- [ ] 3.4 Release C2: rename `exomem-cloud` to `evermatter-cloud` with a `renames` entry, and move its address to `https://mcp.evermatter.ai/mcp`, once the Cloud serves that host and the service notice has gone out.
- [x] 3.5 Add `evermatter setup --migrate` (D5) for stdio registrations, and skip each URL registration. Make doctor list user-written rules and hooks that name the old prefix in a client with no `exomem` server.
- [x] 3.6 Write the new managed-block markers and the readiness service name `evermatter`.
- [ ] 3.7 Document `EVERMATTER_*` as the settings prefix once 2.2 ships. Update the README, docs and skill descriptions, keeping "formerly Exomem" as a trigger. Write the release notes and the service notice for Cloud users.
- [x] 3.8 Prove the switch end to end:
  - a fixture home with the old configuration migrates;
  - a real Claude Code session lists `mcp__evermatter__*`;
  - the capture hook fires;
  - recall answers;
  - an old plugin install moves through `renames`.

  Done on 2026-11-25 on main db7cb81e with Claude Code 2.1.332 and claude-sonnet-5-5, in scratch homes:
  - `setup --migrate` renamed the stdio and non-Cloud URL registrations in Claude Code and Codex, and printed each sign-in step. It kept the Cloud URL registrations and listed other projects' local registrations. A rerun changed nothing.
  - A headless session used the migrated stdio registration for its tools and the evermatter plugin for its hooks. It listed the `mcp__evermatter__*` tools, the retrieve and capture hooks fired, and `search_memory` found a seeded note.
  - After a marketplace update, `renames` moved `enabledPlugins` and `pluginConfigs`.
  - A directory marketplace loads the renamed plugin in place. A Git-hosted marketplace drops the old install. The plugin then stays "not cached" until the person runs `/plugin install evermatter@exomem` once, as the Claude Code marketplace docs state. The release notes for C1 and C2 must say so.
  - The plugin-shaped tool names (`mcp__plugin_evermatter_evermatter__*`) were seen only in `claude mcp list`, because a session against the real HTTP service needs its OAuth setup.
- [x] 3.9 Run skill activation on real Claude models with "Exomem" and "Evermatter" prompts, old names against new. Done on 2026-11-25 with `claude plugin eval` (Claude Code 2.1.332, claude-sonnet-5-5, one draw, a deterministic Skill-call grader) on 10 prompts, half naming each product: 10 of 10 activated with the old skills and 10 of 10 with the new.

## 4. Listings

- [ ] 4.1 Operator: rename the Claude directory and ChatGPT app listings.

## 5. Compatibility window (optional for archive)

Archive this change after `rename-to-evermatter` and before `rename-stored-identifiers-to-evermatter`, because all three modify the same requirement.


- [ ] 5.1 On or after 2027-11-23, review use of the old names and remove them from the accepted sets only with evidence.
