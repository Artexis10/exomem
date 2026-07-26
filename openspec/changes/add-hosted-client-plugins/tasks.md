## 1. Pin Package And Compatibility Contracts

- [x] 1.1 Add failing pure tests for the canonical Hosted definition schema, fixed production HTTPS endpoint policy, `hosted-alpha-agent-v1` identity, strict semantic versions, normalized paths, and rejection of tenant/secret/local-runtime fields.
- [x] 1.2 Add failing reproducibility tests for canonical-definition, skill-set, command-surface, schema-contract, platform artifact, and package-lock digests, including stale-input and development-channel rejection.
- [x] 1.3 Implement the canonical Hosted definition and typed loader under `plugins/hosted/`, plus deterministic compatibility-lock generation from the Exomem agent contract.
- [x] 1.4 Publish a machine-readable candidate descriptor that the paired Substrate change can import without copying command membership or user-specific data.

## 2. Author The Hosted-Safe Skill Set

- [x] 2.1 Add failing tests that extract callable references and require each skill's declared/observed dependencies to be a subset of the exact alpha profile, with explicit failures for broad edit/replace, transfer, media, adoption, maintenance, schema, coordination, and Tier-2 references.
- [ ] 2.2 Add failing behavior fixtures for quiet project-memory retrieval, useful-note citation, durable decision/failure/pattern/research capture without an explicit save prompt, fresh-chat continuation, redundant/trivial-write avoidance, and no transcript dumps.
- [x] 2.3 Hand-author the generic Hosted core, capture, continue, reflect, research, and review skills under `plugins/hosted/skills/`, keeping every advertised workflow complete on the alpha profile.
- [ ] 2.4 Add bootstrap/profile parity and no-private-leak gates covering all Hosted skill prose, references, examples, generated copies, metadata, and assets.

## 3. Render And Validate Native Packages

- [x] 3.1 Add failing golden/schema tests for the Claude package layout, remote MCP declaration, skills/assets, public metadata, and absence of local stdio/hooks/setup requirements.
- [x] 3.2 Implement deterministic Claude Hosted rendering and validation into a dedicated generated package without changing `plugins/claude-code/`.
- [x] 3.3 Add failing golden/schema tests for `.codex-plugin/plugin.json`, `.mcp.json`, skills/assets, marketplace entry, `ON_INSTALL` authentication, and required OpenAI interface metadata.
- [x] 3.4 Implement deterministic OpenAI Hosted rendering, plugin validation, and pending distribution metadata using the canonical definition.
- [x] 3.5 Add one check/regenerate command that fails on uncommitted generated drift, missing assets/legal URLs, unsupported manifest fields, placeholders, unsafe archive entries, or a package-lock mismatch.

## 4. Implement Promotion Evidence

- [ ] 4.1 Add failing tests for per-platform `pending`/`live`/`failed` transitions, exact evidence identity, atomic replacement, demotion, and rejection of validator-only, OAuth-only, discovery-only, bootstrap-only, metadata-only, or mocked-client evidence.
- [x] 4.2 Implement the promotion record and maintainer commands that bind platform/client version, plugin version, endpoint, profile fingerprint, contract digest, test identity, timestamp, and redacted result hashes.
- [ ] 4.3 Add content-bearing smoke assertions for seeded recall and citation, ordinary-conversation durable capture, and recall from a later fresh conversation.
- [x] 4.4 Make release/distribution metadata expose only live platform artifacts and prohibit the cross-client-ready label until both platforms pass against one compatibility identity.

## 5. Exercise The Paired Product Journey

- [x] 5.1 Add a shared versioned acceptance-fixture format and run identifier compatible with Substrate's `add-exomem-hosted-mcp-oauth` evidence, including exact identity/tenant/operation/cell/volume count assertions.
- [ ] 5.2 Run the static package, dependency, bootstrap, archive-safety, reproducibility, no-leak, and generated-drift suites for both artifacts.
- [ ] 5.3 Install the pending Claude artifact in a clean supported account and record native install, one-login authorization, exact discovery, unprompted seeded content recall/citation, durable capture, and fresh-chat recall evidence.
- [ ] 5.4 Install the pending OpenAI artifact in a clean ChatGPT/Codex account and record the same content-bearing journey rather than accepting connection or tool listing alone.
- [ ] 5.5 Authorize the other client as the same identity and prove both see the same memory while Substrate reports no second tenant, entitlement, provision operation, cell, or volume.
- [ ] 5.6 Run the paired duplicate/concurrent callback, invite expiry/replay, provisioning delay/failure, capacity, stale discovery, cell mismatch, refresh/revocation, suspension/deletion, and cross-tenant sentinel matrix within configured budgets.

## 6. Document And Verify The Friends Release

- [x] 6.1 Document pending distribution, authorization expectations, demotion/rollback, support references, privacy/terms metadata, and the boundary between friends design-partner feedback and market validation.
- [ ] 6.2 Run changed-file formatting/lint, focused and lean tests, package validators, archive inspection, generated-drift checks, and `openspec validate add-hosted-client-plugins --type change --strict --no-interactive`.
- [ ] 6.3 Complete an independent security/package review and verifier pass, resolve findings, and promote only the exact evidence-bound Claude/OpenAI artifacts that pass the real-client matrix.
