## 1. Establish the current behaviour

- [ ] 1.1 Assert that `[[Target#Some Unit]]` in a `- relations:` row today produces a page-level edge with no diagnostic, and that `normalize_wikilink` preserved the fragment before `_with_md` removed it.
- [ ] 1.2 Assert the same for a canonical `## Relations` note-level bullet.

## 2. Resolve the fragment

- [ ] 2.1 Preserve the fragment through target resolution instead of splitting it off in `_with_md`, without changing `_with_md`'s other callers.
- [ ] 2.2 Resolve a fragment through `_current_unit_status`, mapping `found` to a unit destination and `missing`, `ambiguous`, and `stale` to the page-level edge.
- [ ] 2.3 Emit the unit-destination edge with the unit node key, and assert the source anchor is unchanged.
- [ ] 2.4 Assert a target with no fragment is byte-identical to today.

## 3. Tell the author

- [ ] 3.1 Add an unresolvable-fragment and an ambiguous-fragment diagnostic beside the existing malformed-relation and unresolved-target counts.
- [ ] 3.2 Assert a typo degrades to a page-level edge and reports, rather than deleting the relation.

## 4. Revisit the consumer

- [ ] 4.1 Propose `shared_open_question` as a unit-level `duplicates` candidate between the two question units, using the `unit_ref` pair its evidence already carries.
- [ ] 4.2 Assert the dismissal fingerprint still expires when either unit is re-anchored.

## 5. Check the consumers of an edge endpoint

- [ ] 5.1 Traversal profiles, `graph-find-ranking`, and the acceptance queue over a graph containing a unit destination.
- [ ] 5.2 Reconcile and rebuild converge on a graph containing unit destinations.

## 6. Contract and surface

- [ ] 6.1 Document the target form in the semantic-authoring contract and bump its version.
- [ ] 6.2 Regenerate the scaffold skill headers, `docs/capabilities.md`, `tests/fixtures/mcp_tool_schemas.json`, and `src/exomem/tool_surface_contract.json`.
- [ ] 6.3 Move `pending_tool_surface_sha256` in `deploy/chatgpt/personal-plugin-contract.json` and state in the PR that the connector remains unverified since 0.45.0 — this change adds to that state and does not clear it.

## 7. Evidence

- [ ] 7.1 `tests/test_latency_gate.py` at 2k and 8k, reported in the PR rather than deferred.
- [ ] 7.2 `openspec validate resolve-relation-fragment-targets --strict` and `openspec validate --specs --strict`.
- [ ] 7.3 The affected suites plus lint.

## 8. Closure

- [ ] 8.1 Once merged and therefore demonstrably shipped, sync the delta into `openspec/specs/` and archive with `openspec archive`, re-running `openspec validate --all --strict` before and after.
