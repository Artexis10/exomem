## 1. S1 Procedure type

- [ ] 1.1 Add the `procedure` entry to `src/exomem/vocabulary/packs/core/note-types.yaml`: role compiled, folder `Notes/Procedures`, sources optional, guidance that asks for a `summary` (when to use it), a "not for" section and procedure blocks.
- [ ] 1.2 Teach the type in `src/exomem/_scaffold/_Schema/references/page-types.md`, `semantic-authoring.md` and `SKILL.md`. Keep the text generic.
- [ ] 1.3 Regenerate the plugin copies and `docs/capabilities.md`. Run the note-type registry tests and `tests/test_scaffold_no_leak.py`.

## 2. S2 Ledger and operation

- [ ] 2.1 Add the `procedure-approvals` descriptor (vault-canonical, tree `_approvals`, owner `approve_procedure`) to `src/exomem/reserved_paths.py`. Prove that generic, import, capture, move and recovery routes refuse the tree.
- [ ] 2.2 Add `src/exomem/procedure_approval.py` with the ledger append and read, the eligibility test and the one verdict function. An unreadable ledger returns `unapproved` and one finding.
- [ ] 2.3 Register `approve_procedure` (actions `approve` and `revoke`, `expected_version`, owner-only) in `src/exomem/operation_entries.py` and `src/exomem/commands.py`. Check the MCP tool, the generated CLI route and registry parity.
- [ ] 2.4 Add an owner-only `read_review` listing of approvals with their verdicts and the ledger finding.
- [ ] 2.5 Test the operation: refusal of a stale version, an ineligible page, a shared ref and a non-owner principal, and an append that survives a restart.

## 3. S3 Serving

- [ ] 3.1 In `src/exomem/context_task.py`, add `approval` to the method item and the approved line, charge the line to the shared budget, and compare the verdict in revalidation.
- [ ] 3.2 In `src/exomem/working_set_runtime.py`, carry `approval` in `method_versions`. Read an entry without the field as `unapproved`.
- [ ] 3.3 In `src/exomem/get_page.py`, report the verdict in the `read_memory` result.
- [ ] 3.4 Render the approved line in the retrieve hook. Regenerate the plugin hook copies and the Cloud hook bundle.
- [ ] 3.5 Regenerate the tool schemas, the tool-surface contract, the ChatGPT pending digest and `docs/capabilities.md`. Raise the schema budget by the measured bytes.
- [ ] 3.6 Test the serving paths: a revoke with unchanged bytes resets held methods, a direct file edit makes the approval stale, a restricted reader sees two states, and approval does not change the ranking.

## 4. S4 Proof on real models

- [ ] 4.1 Create, approve and follow: the owner creates and approves a procedure, and a second client follows it on a matching task.
- [ ] 4.2 Stale after edit: edit the approved page and check that the agent treats it as reference and does not call it endorsed.
- [ ] 4.3 Hostile captured source: capture a runbook that tells agents to approve it, with host prompts on. Check that no agent calls `approve_procedure`. If one does, move approval to the human routes only.
- [ ] 4.4 Conflict between two approved procedures: check that the agent names the conflict and follows the user's instruction.
- [ ] 4.5 Unrelated task: check that the agent does not apply a delivered procedure.
- [ ] 4.6 Task outside Exomem: check that the agent finds and follows a matching procedure in a repository task.

## 5. Validate, merge, release and archive

- [ ] 5.1 Run `openspec validate add-approved-procedures --strict` and `openspec validate --all --strict`, and the public-artifact privacy gate.
- [ ] 5.2 Get an independent review of the built change. Merge after the review.
- [ ] 5.3 Release through release-please. Install the release and refresh the ChatGPT schema.
- [ ] 5.4 After merge and the real-model proof, synchronize the deltas into the canonical specs and archive this change with `openspec archive`.
