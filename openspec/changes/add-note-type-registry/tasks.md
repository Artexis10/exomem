# Tasks

S4a starts implementation after `add-lifecycle-status-registry` merges. S4b follows S4a.

## 1. S4a: registry and read side

- [x] 1.1 Rebase on merged S3; refresh the modified requirements against its synchronized text, keeping every S3 scenario. Verify `openspec validate --all --strict`.
- [x] 1.2 Red first: add the workflow test for an overlay compiled type with its own folder. `find` boosts it, the write gate requires a unit, and activation admits it; a restore leaves its pages as debt.
- [x] 1.3 Add the `note_types` adapter, pack and `RegistrySpec` as subject `note-types`; give the closed role set its C6 reason. Verify save, history, restore and the immutable attributes through real registry operations.
- [x] 1.4 Add the operation basis and predicates under S3 admission. Verify pack parity for sets A to H, shipped classification under each overlay state, and denied absent and present equivalence.
- [x] 1.5 Replace each literal copy of sets A to E, G and H with a predicate call, keeping each site's normalisation. Derive the typed writer's accepted types from the shipped pack, expand roles per SQL query, and delete the dead `semantic_writes` set. Verify that the existing suites pass unchanged.
- [x] 1.6 Read the semantic contract's destinations from the registry; verify compiled intent, both mismatch findings and a refused duplicate folder.
- [x] 1.7 Move the role-to-knob map into `ranking_config`; delete the duplicate `find_policy` constants and the `find` aliases. Verify that the ranking, auto-tune and bounded-pass tests pass unchanged.
- [x] 1.8 Count usage from the graph's file nodes by type, and report admitted unregistered types as audit debt. Verify `unavailable` on a cold projection and no debt for product types.
- [x] 1.9 Add the registry's effective digest to the catalogue identity. Verify that a save and a restore reach the next `find` and write without a restart or rebuild.
- [x] 1.10 Teach the role rule in contract version 7, render the shipped list from the pack and fix `search_guidance.compiled_types`; remove the `COMPILED_TYPES` sentences. Verify skill-contract validation, the no-leak scan and the bootstrap budgets.
- [x] 1.11 Regenerate the scaffold copies, plugins, cloud plugin and v5 candidate through their producers; verify the frozen v1 to v4 identities and the legacy contract digest.
- [x] 1.12 Turn the workflow test green and run the full suite at the completion boundary.
  Evidence: S4a merged in #1647 (`fb74f8c57`) with `test_a_vault_compiled_type_takes_compiled_behaviour_until_restored` green.
- [x] 1.13 Obtain an exact independent review that names C4 and C6, the product and proof weight and a net call. Pass full CI, merge and verify the ordinary release.
  Evidence: #1647 merged after review and CI and shipped in the ordinary release that followed it.

## 2A. Homes and shared type folders (organisation PR A)

- [x] 2A.1 Add the `homes` registry adapter, empty pack and `RegistrySpec`, with folder validation through `compiled_route_exempt` and one home per topic or project key. Check: save, history and restore through `schema_memory(subject="homes")`; a reserved segment and a duplicate key refuse.
- [x] 2A.2 Let a type folder accept any compiled type and a compiled type sit anywhere under Notes; reword the two findings. Check: an insight in `Notes/Research/<Project>` is accepted; an untyped page in `Notes/Insights` refuses; a compiled page outside Notes refuses.
- [x] 2A.3 Replace the per-type branches of `note._resolve_path` with the placement order, keeping partitions and stems for the type default, and keep a superseding page in its predecessor's folder. Check: a topic home receives an insight and a failure; with no homes, every destination is unchanged.
- [ ] 2A.4 Refuse a home whose governance or vocabulary-instance scope differs from the type default's (`HOME_CHANGES_SCOPE`), then run `page_scope` on the final path. Check: the refusal writes nothing.
- [x] 2A.5 Report `placement` in the creation draft, with the folder, the basis and the home-making call; route a near-miss topic with a home through the vocabulary decision.
- [x] 2A.6 Count notes by top-level Notes folder with registry labels and give homes index sections; make the research-folder contradiction demotion a project-key rule. Check: a home's pages appear in `Notes/index.md` and in `index_drift`.
- [x] 2A.7 Update the authoring contract's role rule and homes text, bump the contract version once, and regenerate the scaffold, plugin and Cloud copies through their producers. Hosted v1 to v4 were retired in #1691, so no frozen carrier remains to verify.

Evidence: PR A implements these tasks in the homes and placement commits; `tests/test_vault_homes.py` passes in the combined integration scope. Carrier checks and the privacy scan pass. The scope-boundary task remains open for independent review; delivery evidence belongs on PR A.

## 2. S4b: write side and promotion

- [x] 2.1 Record the S4b decisions for partition kinds, stem kinds, the `fields` shape, typed-field declaration and index entries in `design.md`. Verify `openspec validate --strict`.
  Recorded in design.md, "S4b decisions (2026-10-26)".
- [ ] 2.2 Red first: add the programme task 4.3 proof. Promote `meeting-note` under `insight`, create one through `remember` in `Notes/Meetings` with compiled behaviour, then restore it away with page bytes kept.
- [ ] 2.3 Add promotion under a parent that supplies role and flags; verify promotion, history and restore through `schema_memory`.
- [ ] 2.4 Move folders, partitions, stems, required fields, statuses, sections, claim sections and typed fields onto the registry for the typed writer. Verify that the existing suites pass unchanged.
- [ ] 2.5 Create the notes index entry and count for a new compiled folder; verify that a promoted type's pages appear in `Notes/index.md`.
- [ ] 2.6 Deferred (2026-10-26) until a vault type declares typed fields; see design.md. Add the generic `fields` parameter to `remember` and `replace_memory` on the current local and Cloud surfaces. Verify the tool budgets and current descriptors.
- [ ] 2.7 Replace the sink exemption of `entity` and `production-log` with a declared attribute; verify the episode sink report on both types.
- [ ] 2.8 Replace the experiment-only lifecycle in audit, artifact-role state and due state with a declared attribute; verify unfinished-experiment findings and artifact-role origins unchanged.
- [ ] 2.9 Move the experiment-owned `outcome` rule onto the registry's typed fields, and add its `note-type-contract` delta; verify every frontmatter write boundary.
- [ ] 2.10 Derive the knowledge-pack primitives from the registry; verify that every shipped pack still loads.
- [ ] 2.11 Move the singular `project` finding for `pattern` onto the registry's typed fields; verify the frontmatter compliance finding unchanged.
- [ ] 2.12 Move each single-key comparison on `entity`, `source`, `evidence` and `collection` to a role or attribute predicate. Verify the affected suites.
- [ ] 2.13 Unify type normalisation across every predicate site and add its requirement here; verify the deliberate result for a mixed-case type at each site.
- [ ] 2.14 Bind the claim store to the note-type registry's effective digest, and remove the S4a exception from the requirement. Verify that a save and a restore change claim scope at the next claim read, without a page write or rebuild.
- [ ] 2.15 Align statuses with the note-type admission rule: a caller who cannot admit the status overlay, and an unbound library call, classify against the shipped pack instead of refusing. Add the `lifecycle-status-registry` delta; verify the denied-caller indistinguishability tests.
- [ ] 2.16 Turn the task 4.3 proof green and run the full suite at the completion boundary.
- [ ] 2.17 Obtain an exact independent review; pass full CI, merge and verify the ordinary release.

## 3. Closure

- [ ] 3.1 After S4b merges and every task above has evidence, synchronize the deltas and archive with `openspec archive`. Run `openspec validate --all --strict` before and after.
