## 1. Measure and propose

- [x] 1.1 Add `scripts/measure-tool-schema-bytes.py` and record the per-tool, per-component measurement.
- [x] 1.2 Identify duplicated prose, restated types and enums, and guidance that belongs in references.
- [x] 1.3 Propose the per-tool cut, the budget, the destination of moved guidance, and the fingerprint and connector-refresh plan.
- [x] 1.4 Owner rules on the five items under "Needs ruling" in `design.md`.

## 2. Implement (after the ruling)

- [x] 2.1 Red first: add `tests/test_tool_schema_budget.py`, show it failing on the untouched base.
- [x] 2.2 Replace the ten semantic-authoring copies with the compact rule in `semantic_authoring.py` and drop it from parameter descriptions.
- [x] 2.3 Loosen the `ask_memory` output schema, keeping the `result` wrap, and update the two retrieval tests to assert the runtime payload.
- [x] 2.4 Drop optional-null structure and shorten the shared `response_detail`, `purpose` and `authorization_session_credential` parameters.
- [x] 2.5 Rewrite the remaining descriptions to their ceilings, keeping refusal codes, guard flags and destructive-operation requirements.
- [x] 2.6 Move the long guidance into the scaffold references and add the reference index to `bootstrap(profile="full")`.
- [x] 2.7 Regenerate, in this order: `scripts/dump-tool-schemas.py`, the hosted v5 candidate and current render, plugin skills, `scripts/generate-capabilities.py`.
- [x] 2.8 Confirm v1 to v4 and command-binding files are byte-identical and the immutability manifest passes.
- [x] 2.9 Report before and after bytes per tool in the PR.

## 3. Close

- [ ] 3.1 Owner refreshes the connector and verifies; then update the attested digest.
- [ ] 3.2 Sync the delta into `command-surface`, run `openspec validate --all --strict`, and archive with `openspec archive`.
