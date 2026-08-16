## 1. Pin The Widened Profile

- [ ] 1.1 Add failing tests for the exact ordered `hosted-alpha-agent-v3` membership, its v2 prefix, Tier-1/`rest` eligibility of `replace_memory`, `plan_memory`, and `edit_memory`, and unchanged v1/v2 membership.
- [ ] 1.2 Add `HOSTED_ALPHA_AGENT_V3_PROFILE` and its `ProductSurfaceProfile` entry to `PRODUCT_SURFACE_PROFILES`.
- [ ] 1.3 Prove the deterministic agent gateway contract and surface descriptor resolve for v3 without changing the v1 or v2 contract digests.

## 2. Generalize Hosted Candidate Plumbing

- [ ] 2.1 Add failing tests that `load_definition`, `skill_dependencies`, `compatibility_manifest`, and the package lock resolve for a third candidate and still pin the records reader floor.
- [ ] 2.2 Replace pairwise `== LIFECYCLE_CANDIDATE` branching with a `CANDIDATE_PROFILES` registry and a `RECORDS_CANDIDATES` predicate.
- [ ] 2.3 Thread the candidate through `_selection_cases` and `_validate_records_acceptance` so a records-bearing candidate is held to its own acceptance expectations.
- [ ] 2.4 Offer every registered candidate to `scripts/hosted-plugin.py --candidate`.

## 3. Ship The v3 Candidate

- [ ] 3.1 Add the v3 definition, records selection cases, and the v3 copy of the `exomem-records` skill.
- [ ] 3.2 Add the `exomem-supersede` skill teaching supersession, planning, and in-place correction, and prove it passes `validate_skill_text` and the Hosted public-input leak gate.
- [ ] 3.3 Add `pending` promotion records for both v3 platforms.
- [ ] 3.4 Render the v3 Claude candidate with `scripts/hosted-plugin.py render` and commit the generated tree.

## 4. Prove Nothing Else Moved

- [ ] 4.1 Add failing tests that v1 and v2 committed generated artifacts still match a fresh render and that the v1 release-identity fixture still verifies.
- [ ] 4.2 Confirm `tests/fixtures/mcp_tool_schemas.json` and `src/exomem/tool_surface_contract.json` are byte-identical to the base revision, and that no ChatGPT Personal Plugin fingerprint moved.
- [ ] 4.3 Run the targeted Hosted, product-surface, connector-guardrail, and tool-surface suites plus Ruff, and record the verbatim output.
- [ ] 4.4 Run `openspec validate widen-hosted-epistemic-surface --strict` and `openspec validate --specs --strict`.
