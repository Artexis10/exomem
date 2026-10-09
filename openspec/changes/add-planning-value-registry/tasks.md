## 1. Registry

- [x] 1.1 Add the `planning-values` pack, adapter and `RegistrySpec` with `class` and `parents`, and register the subject. Evidence: `src/exomem/planning_values.py`, `vocabulary/packs/core/planning-values.yaml`; `test_vocabulary_registries.py` passes.
- [x] 1.2 Validate the governed fields through the registry in `normalize_item`, keep stored values readable, and pass the vault root at every call site. Evidence: `test_a_vault_status_is_used_classified_archived_and_reverted`.
- [x] 1.3 Move the archive rule, the audit open-item rule and the parent-kind rule onto `class` and `parents`. Judge registry `parents` only on the item a write changes when the change can tighten them, and on the direct children of a kind change; keep shipped kind rules for stored items. Record the remaining key rules as debt in the design. Evidence: `test_registry_parents_bind_only_the_writes_that_depend_on_them`.
- [x] 1.4 Read the bootstrap lists, default horizon views and saved-view horizon check from the registry; quote horizon scalars YAML would retype; keep frozen profiles on the shipped lists. Evidence: the scaffold step of the first workflow test; `test_bootstrap_frozen_profiles.py` passes.
- [x] 1.5 Classify the due-state write delta server-side, as its unfiltered snapshot reads. Evidence: `test_a_restricted_write_does_not_reopen_an_item_a_vault_status_settled`.

## 2. Proof and delivery

- [x] 2.1 Workflow tests through the tool surface: a vault status used, classified, archived and restored; a kind registered again with other parents, a re-kind that would strand a child, and cleanup after a kind is removed; a restricted write on a settled item.
- [x] 2.2 Run the Planning, audit, due-state, collection-store, bootstrap budget and frozen-profile suites unchanged apart from passing `vault_root` explicitly.
- [x] 2.3 Regenerate the skill contract, tool schemas, plugins, cloud plugin and hosted development render; run ruff, OpenSpec strict and the privacy gate.
- [ ] 2.4 Merge to main and ship it in the next release.
- [ ] 2.5 After the release, sync the delta into `openspec/specs/planning` and archive this change with `openspec archive`.
