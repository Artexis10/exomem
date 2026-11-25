# Tasks

## 1. Release A: accept both names

- [x] 1.1 Define the accepted server names beside `client_config.SERVER_NAME`. Make the Codex lookup, the Codex block merge and setup's Claude registration lookup and removal recognise either name.
- [x] 1.2 Recognise the `evermatter` managed instruction-block markers beside the `exomem` ones. Keep writing the old markers.
- [x] 1.3 Accept either readiness service name in doctor, from one definition beside the readiness payload.
- [x] 1.4 Accept either server name in the Codex auth session harness.

## 2. Settings prefix (D3)

- [ ] 2.1 Accept `EVERMATTER_*` per source: the process environment (fill only when unset, log a conflict), the `.env` loads before their conflict check, the standalone hooks' alias normalisers, setup's `.env` route lookup and the early resource preload.

## 3. Release B: emit the new names

- [ ] 3.1 Operator choice on D6 (Cloud plugin sign-in) recorded in the design.
- [ ] 3.2 Emit the server key and serverInfo name `evermatter`. Rename the skills to `evermatter-*` in the workflow-skill index and the core skill. Regenerate the plugin trees.
- [ ] 3.3 Add `renames` to `marketplace.json` for `exomem` → `evermatter` and `exomem-cloud` → `evermatter-cloud`. Set the marketplace display name. Run `claude plugin validate .`.
- [ ] 3.4 Move the Cloud plugin's address to `mcp.evermatter.ai/mcp` once the Cloud's accepted resource list serves it.
- [ ] 3.5 Add `evermatter setup --migrate` (D5). Make doctor list user-written rules and hooks that still name the old prefix.
- [ ] 3.6 Write the new managed-block markers and the readiness service name `evermatter`.
- [ ] 3.7 Document `EVERMATTER_*` as the settings prefix once 2.1 ships. Update the README, docs and skill descriptions, keeping "formerly Exomem" as a trigger. Write the release notes and the service notice for Cloud users.
- [ ] 3.8 Prove the switch end to end:
  - a fixture home with the old configuration migrates;
  - a real Claude Code session lists `mcp__evermatter__*`;
  - the capture hook fires;
  - recall answers;
  - an old plugin install moves through `renames`.
- [ ] 3.9 Run skill activation on real Claude models with "Exomem" and "Evermatter" prompts, old names against new.

## 4. Listings

- [ ] 4.1 Operator: rename the Claude directory and ChatGPT app listings.

## 5. Compatibility window (optional for archive)

- [ ] 5.1 On or after 2027-11-23, review use of the old names and remove them from the accepted sets only with evidence.
