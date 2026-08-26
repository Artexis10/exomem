## 1. Pin The Canonical Profile

- [x] 1.1 Add failing pure tests for the exact ordered `hosted-alpha-agent-v1` membership, forbidden broad operations, Tier-2 exclusion, and unknown-profile rejection.
- [x] 1.2 Add the immutable set-level profile registry beside canonical product commands and implement the fail-closed profile resolver.

## 2. Derive The Agent Contract

- [x] 2.1 Add failing tests for deterministic agent-contract generation, canonical MCP input-schema/annotation fidelity, descriptor metadata/fingerprint parity, full contract digest, and unchanged default private-contract shape.
- [x] 2.2 Implement the Hosted agent descriptor and additive MCP-ready derived gateway-contract builder from canonical bound commands.

## 3. Prove Bootstrap And Compatibility

- [x] 3.1 Add failing tests that extract callable references from compact, full, and diagnostics bootstrap payloads and require every reference to belong to the active profile.
- [x] 3.2 Add failing authenticated ASGI tests for the profile contract/command routes, allowed dispatch, excluded-command rejection before invocation, exact bootstrap descriptor binding, and unchanged legacy routes.
- [x] 3.3 Implement the additive private agent routes by sharing existing auth, coercion, admission, idempotency, and error-envelope behavior; preserve useful filtered bootstrap guidance.
- [x] 3.4 Run focused tests, the platform-neutral lean suite, Ruff, strict OpenSpec validation, and an independent review; document the pre-existing Windows-only hosted-route baseline separately.
  - Focused Hosted/registry/bootstrap/MCP suite: 71 passed on the rebased branch; frozen route/security regression: 10 passed.
  - Linux-native frozen lean run: 4,721 passed and 62 skipped. Its three unrelated failures were qualified against untouched `origin/main`: two transfer tests pass in isolation on both revisions and expire only after the suite exceeds their module-level five-minute grant fixture; one macOS installer test fails identically under WSL's injected Windows `PATH` and passes with a native Linux `PATH`.
  - Changed-file Ruff, compileall, diff check, lock check, and strict OpenSpec validation passed.
  - Independent verifier reported no remaining code findings after the broad-mutation denial regression covered both `edit_memory` and `replace_memory` while active and quiesced.
  - The broader Windows baseline remains unsuitable as a green gate because pre-existing POSIX ownership/mode assumptions, invalid-byte filename collection, and missing `fcntl` fail outside this diff.
