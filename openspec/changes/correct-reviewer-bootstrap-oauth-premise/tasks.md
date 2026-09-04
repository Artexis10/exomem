## 1. Remove the impossible token exchange

- [x] 1.1 Verify all four server-side bars against `oauth-store.ts` before changing anything, and stop if any does not match.
- [x] 1.2 Add a red test proving `run` reaches the canary-credential calls and writes `bootstrap-outcome-final.json` with no token request recorded.
- [x] 1.3 Delete the token-exchange block, the fixture-seeding block, `HostedMCPToolCaller` and its callerless decode tail, and the `re` import and MCP constants they used.
- [x] 1.4 Drop the `fixture` payload from `load_locks` while keeping the file read that feeds `fixture_version` and `fixture_digest`.
- [x] 1.5 Correct the module docstring, `run` docstring, sealing comment and completion message that stated the disproved premise.
- [x] 1.6 Delete the five tests asserting the impossible behaviour and trim the three helpers that fed them.

## 2. Make the OpenAI sibling opt-in

- [x] 2.1 Verify `promoteExomemHostedCohort`'s optional `openaiArtifactId` and its four `promoteOpenai` gates against `agent-contract-store.ts`.
- [x] 2.2 Add a red test proving a connector-less `run` produces exactly one sibling stage and one canary credential.
- [x] 2.3 Add a red test proving a Claude-only `promote` body omits `openaiArtifactId` and `openaiEvidence`.
- [x] 2.4 Make `--openai-connector` optional and build the sibling list from it.
- [x] 2.5 Build the promote body from what the state directory holds, refusing a half-present OpenAI pair.

## 3. Non-resumability

- [x] 3.1 Establish whether an operator-authenticated capability can reclaim a stranded reviewer tenant.
- [x] 3.2 Record the finding in the spec and in operator documentation instead of shipping a `reset` that cannot release the tenant.
- [ ] 3.3 BLOCKED: add `reset` once the control plane exposes operator-authenticated reviewer-tenant reclamation. Needs a substrate change; out of scope here.

## 4. Documentation and closure

- [x] 4.1 Rewrite the reviewer handoff section of `docs/hosted-client-plugins.md`.
- [x] 4.2 Supersede `repair-marketplace-reviewer-fixture`, carrying its executable-fixture requirement forward unchanged.
- [x] 4.3 Run the focused reviewer-bootstrap and fixture-execution suites, Ruff on the changed files, and strict OpenSpec validation.
