## 1. Session Token and Record Contract

- [ ] 1.1 Add failing pure-logic tests for versioned bearer parsing, entropy shape, HMAC verification, constant-time mismatch handling, record context validation, fingerprinted signing-key rotation, and refusal to start HTTP OAuth without explicit signing-key and immutable-user-ID anchors.
- [x] 1.2 Implement the opaque token codec, purpose-separated key derivation, and typed session/generation records without storing raw bearer material.
- [x] 1.3 Add failing tests that distinguish invalid/revoked records from session-authority failures and cover the concurrent revoke-all/issuance generation race.

## 2. Authoritative Session Storage

- [x] 2.1 Add failing storage tests for encrypted single-node persistence, fingerprinted namespaces, remote cross-replica reads, uncached generation checks, permanent tombstones, key-rotation 401 behavior, and stale-local non-resurrection.
- [x] 2.2 Implement `SessionAuthority` with encrypted local and remote-canonical HA backends, fingerprinted collections, permanent tombstones, atomically initialized/randomly replaced generations, and fail-closed exceptions.
- [x] 2.3 Add bearer-authenticated collection enumeration to the internal coordinator state API and remote storage adapter, with tests proving HA refuses an empty storage credential and enumeration exposes no decrypted bearer material.

## 3. OAuth Exchange and Request Validation

- [ ] 3.1 Add failing IdP-callback and authorization-code tests for configured GitHub login plus configured immutable ID, disabled verifier caches, wrong/missing identity rejection, token-free client codes, downstream MCP scope preservation, omitted lifetime fields, abandoned/invalid-PKCE flows, and one-time code use.
- [ ] 3.2 Implement `ExomemSessionOAuthProxy` callback proof extraction plus authorization-code exchange so the callback stores no GitHub token and exchange commits a generation-stable local session without upstream-token/JTI state.
- [ ] 3.3 Add failing request-validation tests proving repeated active-session requests make zero GitHub calls, survive simulated GitHub revocation and more than ten authorizations, and reject malformed/context-mismatched tokens.
- [ ] 3.4 Implement opaque bearer loading against `SessionAuthority`, returning an `AccessToken` from stored MCP context without checking GitHub or current login configuration.
- [ ] 3.5 Add failing tests for callback cleanup success, identity/proof/redirect failure, abandoned downstream code, already-revoked tokens, transient cleanup failure, disabled raw-token caches, and secret-safe logs.
- [ ] 3.6 Implement exact-token GitHub cleanup inside the callback, treating already-invalid tokens as cleaned, discarding raw credentials before client-code persistence, and alerting on transient cleanup failure.

## 4. Revocation and Availability Semantics

- [ ] 4.1 Add failing tests for RFC 7009 client revocation, single-session operator revocation, global generation replacement, immediate cross-replica propagation, and concurrent issuance safety.
- [ ] 4.2 Enable local OAuth revocation and implement client, single-session, and revoke-all operations through the same session authority.
- [ ] 4.3 Add failing HTTP-boundary tests proving invalid/revoked/prior-key tokens return 401 with the normal challenge while current-authority network or corruption failures return 503 without `WWW-Authenticate: invalid_token` or another OAuth challenge.
- [ ] 4.4 Implement the narrow auth-store-unavailable mapping at the HTTP boundary and ensure FastMCP's adapter does not swallow infrastructure exceptions.

## 5. Operator Surface and Migration Controls

- [ ] 5.1 Add CLI tests for `exomem auth sessions`, `exomem auth revoke <session-id>`, and `exomem auth revoke --all`, including authorization/config errors and secret-free output.
- [ ] 5.2 Implement the operator-only auth commands over the local or remote session authority without registering them as MCP knowledge tools.
- [ ] 5.3 Add migration tests proving legacy FastMCP JWTs are not dual-read, connector URLs/discovery/DCR/PKCE remain unchanged, and rollback can use preserved legacy state during a bounded window.
- [ ] 5.4 Extend remote setup to generate/preserve `EXOMEM_JWT_SIGNING_KEY`, resolve/persist `EXOMEM_GITHUB_USER_ID`, and generate/configure matching non-empty coordinator OAuth-storage credentials; add offline and probed doctor validation.
- [ ] 5.5 Document the coordinated replica cutover, one-final-login expectation, new required settings, rollback procedure, post-window legacy JTI/upstream-record cleanup, and updated troubleshooting semantics.

## 6. Dependency and Client Compatibility Gates

- [ ] 6.1 Pin `fastmcp==3.4.4`, refresh the lockfile, and add adapter contract tests for every private callback, transaction/code-store, token, revocation, initialization, and exception seam Exomem relies on.
- [ ] 6.2 Add an automated black-box Codex CLI acceptance test or reproducible harness that logs in once and reuses the stored bearer across fresh processes without a browser prompt.
- [ ] 6.3 Run the equivalent supported hosted-connector smoke test and record the rollout result; block deployment if omitted `expires_in` is not persisted correctly.
- [ ] 6.4 Run focused auth/coordinator/CLI tests, the full lean pytest suite, strict OpenSpec validation, and Ruff; resolve every failure before review.
- [ ] 6.5 Have an independent reviewer verify the implementation against every scenario in `durable-mcp-auth-sessions`, including zero post-issuance GitHub calls and 503-vs-401 behavior.
