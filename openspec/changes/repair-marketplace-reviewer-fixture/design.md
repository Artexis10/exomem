## Context

The marketplace release tree contains a deterministic reviewer fixture and statically binds review cases, directory packets, reviewer credentials, and evidence to its version and payload digest. Static validation currently proves only JSON shape, reference consistency, and hashes. It never executes the payload against `remember`, so two prose-only notes survived release checks even though semantic-authoring v4 rejects them with `missing_semantic_unit`.

The reviewer bootstrap already obtains a normal setup-session cookie plus an OAuth access/refresh token when it redeems the dedicated invite. The current driver then creates provider credentials immediately. The control plane seals that setup session and token graph as soon as either credential is issued, which makes the required pre-seal fixture seeding impossible. The staged release, not the consumed bootstrap authority, owns the remaining evidence lifetime; current operational measurements put cell provisioning at roughly eight to ten minutes.

The repair spans a checked release artifact, the normal governed write path, the operator bootstrap transport, and release tests. It does not need a new server-side capability or privileged mutation endpoint.

## Goals / Non-Goals

**Goals:**

- Make the exact checked fixture executable under the current semantic-unit and relation-review contracts.
- Detect future fixture/contract drift locally before a candidate or reviewer window exists.
- Seed and verify the fixture automatically through the same Hosted MCP product surface a normal authorized client uses.
- Preserve the setup authority until the fixture is durably verified, then seal it by issuing both provider credentials.
- Keep secrets and content-bearing MCP responses out of console output and ordinary operator receipts.

**Non-Goals:**

- Weakening semantic-unit or relation-review requirements, adding fixture exemptions, or writing directly to a vault/database outside the product command.
- Automating or fabricating native ChatGPT/Claude acceptance evidence, provider portal submission, or publication.
- General bulk-import or customer-onboarding fixture support.
- Changing the MCP command surface, OAuth protocol, tenant model, or provider credential contract.
- Solving evidence-window sizing already owned by the separate promotion-window tooling change.

## Decisions

### Publish a v2 fixture whose Markdown is natively valid

Replace the v1 fixture with v2 and update every binding rather than silently changing content beneath the old fixture identity. Preserve the facts and review intents, but express the two invalid notes with natural recognized rich units: the project state becomes a `## Finding` and the review instruction becomes a `## Requirement`. Seed dependency targets before notes that link to them.

The payload remains generic and deterministic. Adding exemption tags, representing compiled conclusions as raw Sources, or weakening the semantic contract was rejected because each would make the review data pass by ceasing to represent the product being reviewed.

### Use one seeding state machine across local and live execution

Add a fixture seeding function next to the existing fixture loader in `hosted_plugins`. It accepts a narrow `call_tool(name, arguments)` callback and has no transport or vault privilege of its own. For each checked pre-seeded note it:

1. calls `remember` with the exact title, slug, body, `note_type="insight"`, `suggestions=false`, and `validate_only=true`;
2. refuses any non-review blocker or malformed draft response;
3. commits the exact unchanged draft, adding only the returned `draft_id`, `draft_hash`, `draft_token`, and, when required, `relation_disposition="reviewed_none"`, the returned relation-review hash, and a bounded generic audit reason;
4. reads the committed path back with `read_memory` and compares its title and body to the fixture before proceeding.

The executable conformance test supplies a callback backed by the real `commands.op_remember`/`op_read_memory` leaves in an initialized temporary vault. The live bootstrap supplies an authenticated HTTP MCP callback. A parser-only schema test or a mock-only seeder test was rejected because either can repeat the same invalid assumption without exercising the write contract.

### Seed after readiness and before provider credentials

Change `reviewer_bootstrap.run` to require a successful token exchange, retain the token only in its existing mode-0600 state directory and process memory, then poll the content-free owner status endpoint with the setup cookie until `state=ready` and `code=CELL_READY`. The wait is bounded and must leave a reserve before the recorded staged-release expiry.

After readiness, the driver invokes the shared seeder through `/api/exomem/mcp/v1`, verifies all v2 notes, and writes a mode-0600 content-free receipt containing only fixture version, payload digest, note count, and verification outcome. Only then may it create the sibling stages/clients and issue Claude/OpenAI internal-canary credentials. “Bootstrap complete” is printed only after both credentials exist.

Waiting in the native clients was rejected because it reintroduces manual operator work. Seeding after credential creation is impossible by contract because credential issuance revokes the setup graph. A control-plane seeding endpoint was rejected because it would create a privileged write path with weaker parity than the product surface.

### Keep the MCP transport secret- and content-blind on disk

The bootstrap's MCP adapter sends bearer material only in the `Authorization` header, accepts JSON or framed SSE, and never routes MCP request bodies or responses through `ControlPlane._record`. It returns decoded domain objects in memory. Errors name the stage and stable protocol outcome without echoing tokens, cookies, note bodies, or raw server responses.

The existing protected state directory remains the recovery boundary for OAuth token material and provider credentials. The new seed receipt is deliberately content-free; native-client acceptance remains the only retained content-bearing proof.

## Risks / Trade-offs

- [Provisioning consumes too much of the staged release] -> Bound readiness polling by the recorded stage expiry, reserve time for seeding/credential issuance, and fail before sealing. The separate window-duration change remains responsible for giving the later human evidence run enough time.
- [A transport failure lands after an MCP commit] -> Use one stable JSON-RPC request ID for each logical call and preserve the unsealed setup authority; do not issue credentials until exact readback proves the complete fixture.
- [A partial seed makes a blind rerun collide] -> Treat existing exact paths as verifiable progress only when readback matches the checked title/body; any mismatch fails closed rather than overwriting it.
- [Fixture content changes deterministic packet identities] -> Bump to v2, update the review-case binding, rerender directory packets, and cut a fresh Hosted candidate/window. Prior v1 evidence remains historical and cannot authorize v2.
- [The automated setup is mistaken for provider acceptance] -> Keep clean-client prompts, native install/OAuth, recall, citation, capture, fresh-chat recall, and signed evidence manual and independently bound.
- [MCP errors leak fixture content or credentials] -> Redact transport failures, never persist MCP bodies, and run public-artifact/privacy plus secret-pattern checks before delivery.

## Migration Plan

1. Add failing executable fixture and bootstrap ordering/fail-closed tests.
2. Introduce the shared seeding state machine and make the v2 payload pass it without semantic exceptions.
3. Add bounded readiness polling and authenticated MCP seeding before reviewer credential issuance.
4. Rerender deterministic directory artifacts, validate all OpenSpec and Hosted release gates, and merge the repair.
5. Cut a new Exomem release/candidate and open a fresh reviewer window; do not reuse the v1 candidate or its spent evidence state.
6. Run genuine ChatGPT and Claude acceptance, import signed evidence, promote the paired cohort, then continue to the separate Paddle paid-flow and invitation gates.

Rollback before credential issuance leaves setup access unsealed and reports failure. Rollback after a new credential exists uses the existing reviewer-credential revocation and reviewer-tenant cleanup procedures; it never restores v1 as authority for v2.

## Open Questions

None. The existing setup cookie, OAuth token response, status endpoint, MCP endpoint, and credential-sealing contract provide every required seam.
