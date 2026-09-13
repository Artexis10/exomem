## 1. Baseline And Red Tests

- [x] 1.1 Run the current focused mutation, command-classification, entity/link, index, pack, bootstrap, hook, and connector-fingerprint tests from the clean v0.24.2 worktree.
- [x] 1.2 Add and run failing tests proving `edit_memory(validate_only=true)` is read-only, bypasses writer/idempotency/mutation admission, and remains usable while another mutation owns the boundary.
- [x] 1.3 Add and run failing real-surface tests for edit semantic-preflight failure, transport cancellation, identical pending retry, terminal replay, and no self-induced `MUTATION_BUSY`.
- [x] 1.4 Add and run failing tests for content-free mutation-holder telemetry and bounded background reconciliation lock ownership.

## 2. Mutation Safety

- [x] 2.1 Integrate the receipt-first replay implementation from PR #252 without changing the public `edit_memory` name or mutation result contract.
- [x] 2.2 Classify only `edit_memory(validate_only=true)` as read-only and keep guarded semantic validation correct without acquiring write authorities.
- [x] 2.3 Add opaque request/operation/holder-kind/age state to the vault mutation coordinator and expose bounded content-free coordination/readiness diagnostics plus long-holder warnings.
- [x] 2.4 Bound file-watcher/media reconciliation mutation batches and release the global boundary between batches.
- [x] 2.5 Run the focused mutation, real command-surface, FastMCP cancellation, lease, hosted admission, and readiness suites green.

## 3. Verification And Review

- [x] 3.1 Run strict OpenSpec validation, Ruff on changed files, targeted type checks, package build/import, and tool-fingerprint verification.
- [x] 3.2 Run the full lean pytest suite on Python 3.13 and required product E2E, retrieval, and package gates.
- [x] 3.3 Request an independent adversarial review of the current mutation-safety implementation against exact base and head SHAs.
- [ ] 3.4 Resolve every critical or important review finding and have the original reviewer verify only the corrected findings.
- [ ] 3.5 Bound the accumulation of never-retried pending receipts: either sweep rows whose owner is provably dead at startup, or report the oldest pending age as the age of the oldest unretried identity rather than as stalled work.
- [ ] 3.6 Give every background boundary holder an opaque per-acquisition request identifier instead of the shared `untracked` label, so two concurrent background holders and a long-holder warning stay distinguishable.
- [ ] 3.7 Decide and test what an identical retry observes when no explicit key and no stable retry scope resolve, so the replay guarantee is either honoured or explicitly declared out of scope for that configuration.

## 4. Closure

- [ ] 4.1 Prove live validate-only overlap, cancelled edit plus identical retry, and bounded holder telemetry without leaving smoke artifacts outside recoverable trash.
- [ ] 4.2 Synchronize the surviving mutation-safety deltas, archive the change through `openspec archive`, and run strict validation before and after.
