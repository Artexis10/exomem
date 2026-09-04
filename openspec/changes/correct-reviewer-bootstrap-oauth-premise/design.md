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

### Do not ship a `reset` command

The intended `reset` cannot be built against the current control plane, so it is not built. Reclaiming a stranded reviewer tenant means deleting it, and deletion is reachable only through `POST /api/exomem/deletion/request` and `POST /api/exomem/deletion/confirm`. Both resolve an owner session rather than the operator bearer token, and `requestDeletionConfirmation` emails a single-use token to the tenant owner's address, so even the session path needs a human to read a mailbox. There is no `admin/tenants` route, and the two deletion functions have no other callers in the tree.

The three reviewer recovery controls the operator surface *does* expose are not substitutes. `recover-terminal-reviewer-delete` requires a delete operation already at `state = 'failed_terminal'`, `error_code = 'LIFECYCLE_MAX_ATTEMPTS'`, `checkpoint = 'destroyed'`, with a consumed `deletion_confirmation` token and a revoked session. `supersede-stranded-cell-delete` requires one at `error_code = 'PROVISIONER_REJECTED'`. `recover-expired-reviewer-cleanup` is likewise keyed on a source operation. Each repairs a deletion already requested and confirmed; none can start one.

A partial `reset` that retired only the spent stage, client and authority was rejected. Those are reclaimable — `fail-stage`, `revoke_reviewer_bootstrap` — but the tenant is what blocks the retry, so a command named `reset` that left it in place would misrepresent what it had done at exactly the moment an operator is deciding whether to spend another invite.

## Risks / Trade-offs

- [The reviewer vault is no longer seeded automatically] -> It never was; the code that claimed to could not run. Seeding is an operator step performed with reviewer access, and `seed_marketplace_review_fixture` remains its executable definition, tested against a real local vault.
- [A Claude-only promotion retires the live OpenAI artifact] -> `promote-cohort` retires live artifacts for both platforms in one statement. This is control-plane behaviour, unchanged by this work, and is correct for a cohort swap; an operator promoting Claude alone should expect the previous cohort to retire whole.
- [The bootstrap remains non-resumable] -> Accepted and documented rather than papered over. A failed attempt still costs an invite, an alias, a stage and a client, and still strands a tenant. Closing this needs an operator-authenticated reclamation capability in the control plane.

## Open Questions

- What operator-authenticated capability should reclaim a stranded reviewer-purpose tenant? It belongs in the control plane, not this harness, and is the single remaining blocker on making the bootstrap resumable.
