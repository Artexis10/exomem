## Why

`repair-marketplace-reviewer-fixture` rests on a premise that is false. Its design states the bootstrap "already obtains a normal setup-session cookie plus an OAuth access/refresh token", and its delta requires the driver to "use the redeemed setup session and OAuth token" to seed the fixture through Hosted MCP before issuing credentials. The control plane cannot issue that token, and never could.

`redeemExomemOAuthBootstrapAuthorization` stamps the authorization transaction with a `reviewer_bootstrap_authority_id`, and the token endpoint's grant lookup carries an explicit `NOT EXISTS` against exactly that column. Three further independent bars agree: the same redeem transaction disables the bootstrap's own OAuth client, so the enabled-client arm can never match; the authorization code is written with a NULL `candidate_id`, so the `internal_canary` arm cannot apply either; and the grant is created `refresh_allowed = false` while the harness required a refresh token. The exchange therefore aborted every run *after* the invite, the authority and the human's setup session had already been spent — the most expensive moment in the procedure.

Two further defects sit in the same harness. Both it and `promotion_evidence.py` force a Claude+OpenAI pair, although `promoteExomemHostedCohort` takes `openaiArtifactId` optionally and gates every OpenAI precondition behind it, promoting the candidate to `live` either way — so a ChatGPT connector was a precondition of promoting Claude at all. And every failed attempt strands a reviewer tenant that blocks the retry, which the harness names in an error message and offers no way to clear.

## What Changes

- Remove the impossible OAuth token exchange from `reviewer_bootstrap.run`, together with the fixture seeding it existed to authenticate and the Hosted MCP caller that carried it. The bootstrap's real product is the consumed authority, whose `outcome_tenant_id`, `outcome_assignment_id` and `outcome_assignment_generation` are written inside the redeem transaction and read back from the admin surface.
- Keep fixture seeding as an executable definition (`seed_marketplace_review_fixture`) exercised against a real local vault, and stop asserting that the bootstrap performs it.
- Make the OpenAI sibling opt-in in both harnesses: `--openai-connector` becomes optional, and `promote` omits `openaiArtifactId`/`openaiEvidence` entirely rather than sending null when there is no OpenAI artifact.
- Record that the bootstrap is NOT resumable and why: reclaiming a stranded reviewer tenant needs a server capability that does not exist.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `hosted-marketplace-release`: Preserve the executable reviewer fixture requirement, replace the disproved pre-seal token/seeding requirement with what the control plane actually supports, and require Claude-only promotion to be a first-class path.

## Impact

- Supersedes `repair-marketplace-reviewer-fixture`. Its "Executable marketplace reviewer fixture" requirement is carried forward unchanged and remains satisfied by `tests/test_marketplace_review_fixture_execution.py`. Its "Pre-seal reviewer fixture preparation" requirement is withdrawn as unsatisfiable; neither was ever synced into the main specs.
- `scripts/reviewer_bootstrap.py`: token exchange, fixture seeding, `HostedMCPToolCaller` and its decode tail removed; `--openai-connector` made optional.
- `scripts/promotion_evidence.py`: `promote` builds a Claude-only or paired body.
- `tests/test_reviewer_bootstrap_cli.py`: five tests asserting the impossible behaviour deleted; helpers trimmed; new coverage for the three corrections.
- `docs/hosted-client-plugins.md`: the reviewer handoff description.

No server change is proposed. All three defects are harness-side; the control plane is correct in each case.
