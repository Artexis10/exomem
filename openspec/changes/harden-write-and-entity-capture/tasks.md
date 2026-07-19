## 1. Baseline And Red Tests

- [x] 1.1 Run the current focused mutation, command-classification, entity/link, index, pack, bootstrap, hook, and connector-fingerprint tests from the clean v0.24.2 worktree.
- [x] 1.2 Add and run failing tests proving `edit_memory(validate_only=true)` is read-only, bypasses writer/idempotency/mutation admission, and remains usable while another mutation owns the boundary.
- [x] 1.3 Add and run failing real-surface tests for edit semantic-preflight failure, transport cancellation, identical pending retry, terminal replay, and no self-induced `MUTATION_BUSY`.
- [x] 1.4 Add and run failing tests for content-free mutation-holder telemetry and bounded background reconciliation lock ownership.
- [x] 1.5 Add and run failing registry tests covering the five core entity kinds, pack-default validation, Organizations, registry-derived indexes, bootstrap guidance, and an intentionally versioned MCP fingerprint.
- [x] 1.6 Add and run failing capture-hook/scaffold tests for bounded exact-match-first entity routing, update-before-create behavior, one-off-name suppression, correct `create-entity` operation, and recognition of modern entity writes.

## 2. Mutation Safety

- [x] 2.1 Integrate the receipt-first replay implementation from PR #252 without changing the public `edit_memory` name or mutation result contract.
- [x] 2.2 Classify only `edit_memory(validate_only=true)` as read-only and keep guarded semantic validation correct without acquiring write authorities.
- [x] 2.3 Add opaque request/operation/holder-kind/age state to the vault mutation coordinator and expose bounded content-free coordination/readiness diagnostics plus long-holder warnings.
- [x] 2.4 Bound file-watcher/media reconciliation mutation batches and release the global boundary between batches.
- [x] 2.5 Run the focused mutation, real command-surface, FastMCP cancellation, lease, hosted admission, and readiness suites green.

## 3. Registry-Driven Entity Capture

- [x] 3.1 Add the immutable central entity registry for person, organization, concept, library, and decision with unique IDs, folders, labels, and aliases.
- [x] 3.2 Refactor entity validation/render routing, initialization, reverse folder lookup, counts, subindexes, and missing-Organizations reconciliation to consume the registry while preserving custom index prose.
- [x] 3.3 Validate every knowledge-pack `default_entity_types` value against the registry and add Organization capture priority to the relevant built-in packs.
- [x] 3.4 Expose registry and selected-pack priorities in bootstrap, fix the stale `entity` versus `create-entity` route guidance, expose the semantic review fields required to commit `edit_memory`, and version the MCP schema/tool fingerprint without renaming the tool.
- [x] 3.5 Update the capture hook, scaffold, workflow skills, and generated/fixture documentation with bounded exact-match-first, update/link-before-create, durable-central-entity, and no-spam guidance.
- [x] 3.6 Run entity/link/index/init/pack/bootstrap/hook/scaffold/filter and connector-guardrail suites green.

## 4. Verification And Review

- [x] 4.1 Run strict OpenSpec validation, Ruff on changed files, targeted type checks, package build/import, and tool-fingerprint verification.
- [ ] 4.2 Run the full lean pytest suite on Python 3.13 and required product E2E/retrieval/package gates.
- [ ] 4.3 Commit the implementation in reviewable mutation-safety and entity-registry units and request an independent adversarial code review against the exact base/head SHAs.
- [ ] 4.4 Resolve every critical/important review finding and have the original reviewer verify only the corrected findings.

## 5. Release And Production Proof

- [ ] 5.1 Bump and prepare the next pre-1.0 minor release, update release notes/contracts without claiming ChatGPT host-router bugs are fixed, and open the PR with exact smoke evidence.
- [ ] 5.2 Merge only after required CI and release guards pass, publish package/image/GitHub artifacts, and verify versions, hashes, and assets.
- [ ] 5.3 Quiesce public mutations, deploy/restart the Windows Exomem service, and verify local/public health, readiness, OAuth discovery, tool fingerprint, writer/coordinator state, and tunnel behavior.
- [ ] 5.4 Prove live validate-only overlap, cancelled edit plus identical retry, organization create/read/edit/link, existing person compatibility, and bounded holder telemetry without leaving smoke artifacts outside recoverable trash.
- [ ] 5.5 Apply the approved desktop-workstation entity/note correction through `edit_memory`, verification-read it, and report the exact persisted result.
- [ ] 5.6 Refresh/promote the ChatGPT connector contract from a fresh session and record separately any host-side `Resource not found` failure that never reaches Exomem.
- [ ] 5.7 Rotate the credentials exposed during diagnostics, update every authorized service destination atomically, restart, and re-run authentication, writer, tunnel, and connector smokes without printing secret values.
