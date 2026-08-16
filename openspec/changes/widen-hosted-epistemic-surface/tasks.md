## 1. Pin The Widened Profile

- [x] 1.1 Add failing tests for the exact ordered `hosted-alpha-agent-v3` membership, its v2 prefix, Tier-1/`rest` eligibility of `replace_memory`, `plan_memory`, and `edit_memory`, and unchanged v1/v2 membership.
- [x] 1.2 Add `HOSTED_ALPHA_AGENT_V3_PROFILE` and its `ProductSurfaceProfile` entry to `PRODUCT_SURFACE_PROFILES`.
- [x] 1.3 Prove the deterministic agent gateway contract and surface descriptor resolve for v3 without changing the v1 or v2 contract digests.

## 2. Generalize Hosted Candidate Plumbing

- [x] 2.1 Add failing tests that `load_definition`, `skill_dependencies`, `compatibility_manifest`, and the package lock resolve for a third candidate and still pin the records reader floor.
- [x] 2.2 Replace pairwise `== LIFECYCLE_CANDIDATE` branching with a `CANDIDATE_PROFILES` registry and a `RECORDS_CANDIDATES` predicate.
- [x] 2.3 Thread the candidate through `_selection_cases` and `_validate_records_acceptance`, resolving the acceptance profile binding through `_candidate_profile` rather than reusing the candidate string.
- [x] 2.4 Offer every registered candidate to `scripts/hosted-plugin.py --candidate`.

## 3. Ship The v3 Candidate

- [x] 3.1 Add the v3 definition, records selection cases, and the v3 copy of the `exomem-records` skill.
- [x] 3.2 Add the `exomem-supersede` skill teaching supersession, planning, and in-place correction, and prove it passes `validate_skill_text` and the Hosted public-input leak gate.
- [x] 3.3 Add `pending` promotion records for both v3 platforms.
- [x] 3.4 Render the v3 Claude candidate with `scripts/hosted-plugin.py render` and commit the generated tree.

## 4. Re-establish The Protected-Tree Control

- [x] 4.1 Probe the real hosted forwarding route and record that `edit_memory` and `replace_memory` commit writes into `_Schema` and `_Governance` under v3.
- [x] 4.2 Add failing tests that a hosted v3 profile refuses both commands against `_Schema` and `_Governance`, that the refusal survives case variation, `..` traversal, `./` prefixes, doubled and backslash separators, trailing separators, nesting, and absolute paths, and that local and read behaviour is unchanged.
- [x] 4.3 Add the protected-tree guard at the hosted command boundary, before lifecycle admission and before the leaf, scoped to hosted surface profiles.
- [x] 4.4 Add `assert_profile_mutations_are_classified` at route registration so a later widening cannot reopen the hole by silence, with a test proving an unclassified mutation refuses to serve.
- [x] 4.5 State the replacement control as a normative requirement and document the superseded v1 requirement plus the validation gap that hid the conflict.
- [x] 4.6 Close the leading-separator bypass: make every path interpretation cumulative rather than exclusive, never fail open on a parse or resolution error, and pin the absolute-shaped shapes against all three guarded commands asserting the 403.
- [x] 4.7 Resolve relative targets against the vault root as well, so a link into a protected tree is refused even when no segment names one, with a red-first test for the symlink shape.
- [x] 4.8 Prove `TARGET_CONSTRAINED_MUTATIONS` per member instead of claiming it, deriving each member's path arguments from the generated tool-schema fixture.
- [x] 4.9 Encode the guarded-set membership rule as `v3 - v2` and record that `plan_memory`'s guard is defence in depth over its own `_require_profile_layer` constraint.

## 5. Close The Value-Blind Credential Exemption

- [x] 5.1 Add failing tests that a real base64url transition token, `x_transition_token=`, and `transition_token=returned` are all refused by the Hosted public-input gate.
- [x] 5.2 Scope the exemption to the documented placeholder rather than the identifier, and confirm exactly one matching occurrence across `plugins/hosted`.

## 6. Prove Nothing Else Moved

- [x] 6.1 Add failing tests that v1 and v2 committed generated artifacts still match a fresh render and that the v1 release-identity fixture still verifies.
- [x] 6.2 Confirm `tests/fixtures/mcp_tool_schemas.json` and `src/exomem/tool_surface_contract.json` are byte-identical to the base revision, and that no ChatGPT Personal Plugin fingerprint moved.
- [x] 6.3 Run the targeted Hosted, product-surface, connector-guardrail, tool-surface, and protected-tree suites plus Ruff, and record the verbatim output.
- [x] 6.4 Run `openspec validate widen-hosted-epistemic-surface --strict` and `openspec validate --specs --strict`.

## 7. Recorded Follow-ups (not in this change)

- [ ] 7.1 Add a write-verb selection matrix disambiguating `remember` / `edit_memory` / `replace_memory`; v3 must not be promoted before it passes.
- [ ] 7.2 Extend the protected-tree guard to the legacy full private command route, or record that route's trust boundary as a deliberate exclusion in a spec of its own.
- [ ] 7.3 Add a CI render/check step for the v3 candidate, matching the v2 step in the release workflow.
- [ ] 7.4 Measure whether `edit_memory`'s 9 KB `operation` `oneOf` survives the ChatGPT action cache before extending v3 to the OpenAI channel.
