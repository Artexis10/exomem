## 1. Contract and security tests

- [x] 1.1 Add red tests for the generated `preserve_artifacts` schema, exact OpenAI file-parameter metadata, registry exposure, and unchanged metadata-free tools.
- [x] 1.2 Add red pure-logic tests for HTTPS URL validation, public-address filtering, redirect validation, filename fallback, count/size bounds, and URL-safe errors/logging.
- [x] 1.3 Add red command tests for eight-file success, mixed per-file outcomes, mutation replay, narrow commit boundaries, append-only collisions, and media reconciliation warnings.

## 2. Canonical implementation

- [x] 2.1 Add the exact four-field client file model and immutable per-command MCP metadata to the generated command surface.
- [x] 2.2 Implement a bounded, credential-free remote artifact fetcher that pins validated public destinations, revalidates redirects, streams to private temporary files, and enforces per-file and aggregate caps.
- [x] 2.3 Implement `preserve_artifacts` over staged files and the existing `preserve_stream` sink, with ordered per-file results, deterministic filename fallback, narrow mutation guards, commit marking, and soft-fail media reconciliation.
- [x] 2.4 Register the command across MCP, REST, OpenAPI, and CLI without exposing it in the hosted-alpha profile or weakening `/upload`.

## 3. Guidance and compatibility

- [x] 3.1 Update bootstrap and command descriptions to route direct file handles through `preserve_artifacts` and retain `transfer_artifact(operation="upload")` as the no-handle fallback.
- [x] 3.2 Update the generic scaffold and operations reference with copyable direct/fallback examples, correcting the stale `mode` parameter and preserving the no-base64 rule.
- [x] 3.3 Refresh schema/tool fingerprints and add assertions that successful preservation—not token minting—is the only byte-transfer success signal.

## 4. Verification and delivery

- [ ] 4.1 Run focused tests, OpenSpec strict validation, Ruff, schema/fingerprint checks, and the broad affected suite with the Windows temp-fixture workaround documented.
- [x] 4.2 Run independent security/code review and end-to-end verification, address findings, and record the remaining live ChatGPT developer-mode eight-file acceptance gate.
- [ ] 4.3 Commit the intended scope, integrate current `origin/main`, push the feature branch, and open a ready Conventional Commit pull request with evidence.
