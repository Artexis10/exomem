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
- [x] 4.10 Extract the page write leaves' rel-form computation into one shared `kbdir` function, call it from `edit._resolve`, `replace._resolve_kb_path` and the guard, and assert the leaves do not re-implement it.
- [x] 4.11 Replace segment-name matching with resolution of the deepest existing ancestor plus containment against the resolved protected roots, keeping the spelling reading for the unresolved remainder.
- [x] 4.12 Make the spelling reading platform-correct: strip trailing dots and spaces, and segment under both Posix and Windows flavours so drive-qualified, UNC and extended-length spellings are covered.
- [x] 4.13 Make fail-closed a property of the whole evaluation rather than of individual arms, with a test that an unreadable argument refuses.
- [x] 4.14 Guard every caller-supplied target argument of a guarded command, adding `plan_memory.collection`.
- [x] 4.15 Widen the classification sweep from `"path" in key` to every string argument in the pinned schema, asserting every non-enumerated protected path unchanged in bytes and membership.
- [x] 4.16 Add a native-NTFS 8.3 test that runs on Windows and skips elsewhere, and state in `design.md` which cases are simulated here.
- [x] 4.17 Narrow the requirement to caller-supplied write *targets*, enumerate fixed-placement relation-review and open-vocabulary registry writes explicitly without exempting them from the guard, and snapshot the protected trees on the success paths.
- [x] 4.18 Evaluate `root / Path(raw)` for collection targets so the reproduction of that leaf's join over-approximates instead of under-approximating on a backslash component.
- [x] 4.19 Replace the identity-and-grep shared-normaliser assertion with a behavioural one, and show all four re-implementation mutants failing.
- [x] 4.20 Judge a page target as the file its leaf opens, so an extensionless spelling of a legitimate page is not refused while its `.md` spelling is allowed.
- [x] 4.21 Correct the `preserve_evidence` docstring claim that it carries no caller-chosen path argument, and the "any exception" fail-closed wording.

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
- [ ] 7.5 Run the hosted protected-tree suite on Windows to confirm the native NTFS 8.3 case; it is the one case this worktree cannot exercise, and `test_guard_refuses_a_native_ntfs_short_name_alias` skips here. Include `_Schema::$INDEX_ALLOCATION/x.md`, the one identified spelling that would fold to something other than `_schema` and is unverified on real NTFS.
- [ ] 7.6 SECURITY: give `preserve_evidence` a `PathGuard`. It writes via bare `write_text()`/`write_bytes()`, so under the in-vault-link threat model the protected-tree guard accepts, attacker-chosen bytes can be planted inside `_Schema` (create-only; `ARTIFACT_EXISTS` blocks overwrite). Pre-existing and shared with v1 and v2, so it is out of scope here, but it is the one place a leaf-constrained command can still reach a protected tree.
- [ ] 7.7 Relocate relation-review artifacts out of `_Schema`. Operational per-page receipts do not belong in the governing-doctrine tree, and their being there is what forces a carve-out into a security control. Needs a data migration for existing vaults and covers `lifecycle_decision_path` and `lifecycle_prepared_path` too.
- [ ] 7.8 Give a tenant a way out of a self-inflicted lock-out: `preserve_evidence` can create `Evidence/_Schema/...`, after which every later `edit_memory` of that ordinary file is a permanent 403 from the hosted surface.
- [ ] 7.9 Re-examine the protected-tree guard if any write path ever stops going through `batch_atomic_write`'s temp-and-rename. Hardlinks are invisible to the resolution reading; an in-place writer would reopen that.
- [ ] 7.4 Measure whether `edit_memory`'s 9 KB `operation` `oneOf` survives the ChatGPT action cache before extending v3 to the OpenAI channel.
