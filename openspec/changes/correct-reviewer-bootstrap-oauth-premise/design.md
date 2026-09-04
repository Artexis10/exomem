## Context

The superseded change assumed the reviewer bootstrap holds an OAuth access and refresh token after redeeming the dedicated invite, and built a Hosted MCP seeding stage on top of that assumption. The assumption was never checked against the control plane. It is false, and four independent server-side bars make it false — any one of them alone would be sufficient:

1. The token endpoint's grant lookup carries `AND NOT EXISTS (SELECT 1 FROM exomem_oauth_authorization_transactions AS bootstrap_transaction WHERE bootstrap_transaction.id = oauth_grant.authorization_transaction_id AND bootstrap_transaction.reviewer_bootstrap_authority_id IS NOT NULL)`. A bootstrap grant is excluded by construction.
2. The redeem transaction's `disabled_client` CTE sets `enabled = false` on the bootstrap's own OAuth client, so the `client.enabled = true` arm of the grant lookup can never match afterwards.
3. The bootstrap's authorization code is inserted without a `candidate_id`, so it is NULL, and the `internal_canary` arm requires `code.candidate_id IS NOT NULL`.
4. The grant is created `refresh_allowed = false`, while the harness required a non-empty `refresh_token` in the response.

What the redeem transaction *does* produce is durable and sufficient: it sets the authority to `state = 'consumed'` and writes `outcome_tenant_id`, `outcome_assignment_id`, `outcome_assignment_generation`, `outcome_operation_id`, `outcome_session_id` and `outcome_grant_id`. Those are readable from the operator surface, and the three the promotion tooling needs are exactly the three it already read.

## Goals / Non-Goals

**Goals:**

- Delete unreachable code whose only effect was to abort the run at its most expensive moment.
- Keep the executable fixture contract, which is true and tested, separate from the bootstrap transport claim, which was false.
- Make Claude-only promotion a first-class path in both harnesses, matching what the control plane already supports.
- State the non-resumability of the bootstrap plainly, in the place an operator reads before spending an invite.

**Non-Goals:**

- Any server change. All three defects are harness-side.
- Automating fixture seeding by another route. Seeding needs reviewer access, which by contract only exists after the credential that seals setup access.
- Inventing a `reset` command. See the decision below.

## Decisions

### Delete the exchange rather than repair it

The access token had exactly one consumer, the fixture seeding call, so removing the exchange removes the seeding stage with it. Nothing downstream depends on either: `promotion_evidence.py` never reads the seed receipt, and the outcome file the promotion tooling *does* read is written from the authority read-back, before the deleted block. `HostedMCPToolCaller` and its JSON/SSE decode tail become callerless and go too. `wait_for_reviewer_cell` stays — it authenticates with the setup cookie, not the token, so it is independent of all of this.

The checked fixture document is still loaded by `load_locks`, because its `fixture_version` and `payload_sha256` are sent verbatim in every canary credential body. Only the payload itself is no longer carried.

### Make OpenAI opt-in rather than defaulting it

`promoteExomemHostedCohort` declares `openaiArtifactId?: string | null` with the docstring "Omit to promote a Claude-only cohort", derives `const promoteOpenai = typeof input.openaiArtifactId === "string"`, and guards every OpenAI precondition with `(${!promoteOpenai} OR (...))`. The candidate reaches `state = 'live'` on either path, and the cohort target carries no platform references at all, so a Claude-only promotion opens admission.

`promote` omits both OpenAI keys rather than sending `openaiArtifactId: null` alongside a real `openaiEvidence`. An explicit null would take the same server path, but the pair describes a cohort that does not exist, and the honest shape is to send neither. A half-present pair — one file without the other — is still refused, because that is an operator mistake rather than a Claude-only promotion.

### Build `reset` on the expired-reviewer-cleanup escape hatch

Two of the three reviewer recovery controls really are inapplicable: `recover-terminal-reviewer-delete` requires a delete operation already at `state = 'failed_terminal'`, `error_code = 'LIFECYCLE_MAX_ATTEMPTS'`, `checkpoint = 'destroyed'`, with a consumed `deletion_confirmation` token and a revoked session; `supersede-stranded-cell-delete` requires one at `error_code = 'PROVISIONER_REJECTED'`. Each repairs a deletion already requested and confirmed.

The third does not follow that pattern, and it is the one that matters. `recover-expired-reviewer-cleanup` has two eligibility branches, and the second is written for the successful-bound case: `source.operation_type = 'provision'`, `state = 'succeeded'`, `checkpoint = 'bound'`, `tenant.status = 'active'`, `tenant.bound_cell_id = cell.id`, with the tenant's sole cell `active`, `bound`, provider-backed, desired-running, `CELL_READY` and routable with matching observed identity. That is precisely the shape that blocks a retry. Its mutation sets the tenant to `deletion_pending`, advances the fence, revokes sessions, tokens, transfers, invites and grants, supersedes lower-fence operations and enqueues a target-free delete. Its eligibility and mutation join no `exomem_access_tokens`, so the emailed deletion-confirmation token is not on this path, and it dispatches under the same `requireRateLimitedExomemOperator` gate the harness already authenticates against. Substrate's own runbook calls it "the one operator-only escape hatch for a reviewer-purpose tenant whose immutable reviewer assignment expired or was ended through the exact existing `fail-assignment` transition".

`reset` therefore runs `fail-assignment` (only when the assignment is still live), then `preflight-recover-expired-reviewer-cleanup`, then `recover-expired-reviewer-cleanup` — the runbook's own order.

Two identifiers are operator-supplied because no admin route reports them: the tenant fence generation and the reviewer assignment version. This is safe rather than merely tolerable: the preflight is a bare `SELECT EXISTS` returning only `eligible`, so a wrong fence produces a refusal and no mutation. It is also blind, which is why the refusal message enumerates the likely causes instead of inviting a retry with altered selectors — the runbook explicitly forbids that.

The target is read off the consumed bootstrap authority record, never from an argument. `outcomeOperationId` is already on the record the harness parses for `outcomeTenantId`. Taking it from there is what makes the safety structural: the runbook forbids naming a tenant, cell, owner, provider operation or capacity identifier as input, and `reset` accepts none of them, so there is no way to point it at something that is not a reviewer bootstrap.

Two tenant shapes remain uncovered, and are documented rather than worked around: the bound branch requires `tenant.status = 'active'`, the unbound branch requires `bound_cell_id IS NULL` at `checkpoint = 'candidate-cleanup'`, so a tenant stranded between them fits neither. `reset` also reclaims only the tenant — the invite, alias, staged release and OAuth client stay spent.

## Risks / Trade-offs

- [The reviewer vault is no longer seeded automatically] -> It never was; the code that claimed to could not run. Seeding is an operator step performed with reviewer access, and `seed_marketplace_review_fixture` remains its executable definition, tested against a real local vault.
- [A Claude-only promotion retires the live OpenAI artifact] -> `promote-cohort` retires live artifacts for both platforms in one statement. This is control-plane behaviour, unchanged by this work, and is correct for a cohort swap; an operator promoting Claude alone should expect the previous cohort to retire whole.
- [`reset` is driven by two blind, operator-supplied identifiers] -> The cleanup preflight is read-only and returns only `eligible`, so a wrong fence or assignment version costs a refusal, not damage. The refusal message enumerates the likely causes because the server's is deliberately non-diagnostic.
- [A failed attempt still costs an invite, an alias, a stage and a client] -> `reset` reclaims the tenant, which is the part that blocks the retry; the rest stays spent. Operator client slots are not reclaimed by any API, which `preflight` already reports.
- [Seeding the reviewer vault now has no tooling at all] -> True, and stated as an open gap rather than implied away. The executable definition and its local-vault test remain; what is missing is an authenticated caller, which the reviewer credential's username/password shape does not directly provide.

## Open Questions

- Should an admin route expose `fence_generation` and the reviewer assignment version read-only? Both are needed by `reset` and reported by nothing, so today they are typed from out-of-band inspection into a blind preflight. This belongs in the control plane, not this harness.
- What should drive `seed_marketplace_review_fixture` against a live reviewer cell, given the reviewer credential is a username/password rather than a bearer token? Until that exists the checked fixture is entered by hand.
- Should the two uncovered stranded shapes — a reviewer tenant that is neither `active` with a bound cell nor at `candidate-cleanup` with no bound cell — get a cleanup branch of their own?
