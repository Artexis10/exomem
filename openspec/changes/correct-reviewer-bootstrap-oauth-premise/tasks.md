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

## 3. Make a failed attempt recoverable

- [x] 3.1 Verify `recover-expired-reviewer-cleanup`'s successful-bound branch, its operator-token dispatch, and the absence of any `exomem_access_tokens` join, against substrate's source and runbook.
- [x] 3.2 Add red tests for release, for the refusal when nothing is releasable, for a non-diagnostic preflight refusal, for the `fail-assignment` ordering, and for a still-active authority.
- [x] 3.3 Implement `reset` as `fail-assignment` -> `preflight-recover-expired-reviewer-cleanup` -> `recover-expired-reviewer-cleanup`, taking its target from the consumed authority record and printing the release plan first.
- [x] 3.4 Make `--profile` per-command rather than global, so `reset` does not demand a matching release checkout mid-incident.
- [ ] 3.5 GAP: no admin route reports `fence_generation` or the reviewer assignment version, so both are operator-supplied and the preflight is blind. Exposing them read-only is a substrate change, out of scope here.
- [ ] 3.6 GAP: a tenant stranded between the two cleanup branches -- neither `active` with a bound cell nor at `candidate-cleanup` with no bound cell -- is covered by neither. Out of scope here.

## 3b. Reviewer vault seeding

- [ ] 3b.1 GAP: `seed_marketplace_review_fixture` has no production caller. The reviewer credential is username/password rather than a bearer token, and the authenticated Hosted MCP caller was removed with the impossible exchange, so nothing drives the executable fixture definition against a live reviewer cell. Seeding is manual until this is built.

## 4. Documentation and closure

- [x] 4.1 Rewrite the reviewer handoff section of `docs/hosted-client-plugins.md`.
- [x] 4.2 Supersede `repair-marketplace-reviewer-fixture`, carrying its executable-fixture requirement forward unchanged.
- [x] 4.3 Run the focused reviewer-bootstrap and fixture-execution suites, Ruff on the changed files, and strict OpenSpec validation.
