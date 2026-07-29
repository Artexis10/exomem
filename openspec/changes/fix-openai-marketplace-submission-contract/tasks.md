## 1. Lock the Current Provider Contract

- [x] 1.1 Add failing tests for the 30-character OpenAI short-description boundary, 128-character starter-prompt boundary, and forbidden public trial/private-alpha language
- [x] 1.2 Add failing tests that OpenAI packets require deterministic tool-annotation justifications and signed recording-prepared evidence without storing a raw recording URL
- [x] 1.3 Add failing tests for a versioned canonical generic fixture payload/digest, known positive-case fixture references, write-case reset semantics, and rejection of stale or unknown references

## 2. Implement Listing and Review Validation

- [x] 2.1 Update canonical definition validation for current OpenAI limits and word-aware public release-language rejection
- [x] 2.2 Add the canonical generic reviewer fixture payload/digest/reset contract and bind positive review cases to its stable version and references
- [x] 2.3 Render and validate per-tool annotation justifications plus the operator-supplied review-recording handoff

## 3. Stage Submission and Public Activation

- [x] 3.1 Add failing evidence/state-machine tests that provider/deployment/fixture/expiry-bound reviewer access is required and reviewer-ready candidates can be submitted/in-review/approved without broad admission while `ready` and `public` stay false
- [x] 3.2 Add signed secret-free reviewer-access evidence plus submission-specific blockers/readiness while preserving existing stronger `ready`, `blockers`, and `public` compatibility semantics
- [x] 3.3 Require the stronger broad-launch gate for the published transition and retain fresh non-reviewer proof for compare-and-swap activation

## 4. Public Artifacts and Handoff

- [x] 4.1 Replace stale OpenAI public listing/release language and align review cases with the generic seeded fixture
- [x] 4.2 Update hosted-client documentation with the reviewer credential, recording, native-client, broad-launch, and manual portal handoff boundaries
- [x] 4.3 Regenerate and deterministically verify the OpenAI package, locks, archive, and directory packet using the registered application ID

## 5. Verification and Delivery

- [x] 5.1 Run strict OpenSpec validation, focused marketplace/package/schema/leak tests, the full Exomem suite, and Ruff
- [x] 5.2 Obtain independent review of the provider contract and readiness-state diff and address actionable findings
- [x] 5.3 Commit only the intended Exomem scope, integrate current remote main, push, and open a ready pull request with verification evidence

The unfinished clean ChatGPT/Codex/Claude acceptance, provider portal submission, recording upload, receipts, non-reviewer post-install proof, and public activation remain operator-controlled tasks in the existing `add-hosted-client-plugins` and marketplace release workflows.
