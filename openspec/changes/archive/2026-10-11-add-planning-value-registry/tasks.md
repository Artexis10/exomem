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
- [x] 2.4 Merge to main and ship it in the next release.
- [x] 2.5 After the release, sync the delta into `openspec/specs/planning` and verify strict OpenSpec checks.

## Released delivery evidence

[PR #1640](https://github.com/Artexis10/exomem/pull/1640) merged as
`5c38c1f2bfa99b5b5cda7b20facee225100c5d1f` after final head
`3d6705475efb22435684af756acaacecd40dd718` passed 28 checks,
with 10 skipped and none failed.
The [exact review](https://github.com/Artexis10/exomem/pull/1640#issuecomment-6086378026)
approved the final batch with no blocking findings and a ship verdict.

The merge is an ancestor of [v0.115.0](https://github.com/Artexis10/exomem/releases/tag/v0.115.0),
commit `5abed4438ae767af5c89ccd1438c63e35df107d8`.
The published PyPI wheel has SHA-256
`b7b9a602d76f056835463edddf8ebc85a0eeb765fd4cc1094f01a1a0f6e6f49d`.
All 573 package members matched both that tag and the installed distribution.
An isolated CPython 3.13.12 run passed the unchanged maintained workflow
`tests/test_planning_values_workflow.py::test_a_vault_status_is_used_classified_archived_and_reverted`.
It proved tool writes, classification, archive, restore, retained reads and refusal of new removed values.
The two registry workflows passed together: 2 passed in 13.88 seconds, with no skips or failures.
The run made no provider calls and used private runtime state that was removed afterward.
This proves the installed release workflow; desktop acceptance and production rollout remain outside this evidence.
Incomplete parent changes remain open.

## Closure operation

Archiving remains required after canonical sync and strict verification.
Run `openspec archive add-planning-value-registry --skip-specs --json` after those checks.
OpenSpec CLI 1.14.0 completed that command on 2026-10-11 and returned
`archivedAs: 2026-10-11-add-planning-value-registry`.
Its `specsUpdated: false` records the skipped mechanical sync; every delta was
already merged into canonical specs and checked for preservation before archive.
