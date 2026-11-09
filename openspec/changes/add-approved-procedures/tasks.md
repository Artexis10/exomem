## 1. S1 Procedure type

- [x] 1.1 Add the `procedure` entry to `src/exomem/vocabulary/packs/core/note-types.yaml`: role compiled, folder `Notes/Procedures`, sources optional, guidance that asks for a `summary` (when to use it), a "not for" section and procedure blocks.
- [x] 1.2 Teach the type in `src/exomem/_scaffold/_Schema/references/page-types.md`, `semantic-authoring.md` and `SKILL.md`. Keep the text generic.
- [x] 1.3 Regenerate the plugin copies and `docs/capabilities.md`. Run the note-type registry tests and `tests/test_scaffold_no_leak.py`.

## 2. S2 Ledger and operation

- [x] 2.1 In `src/exomem/reserved_paths.py`, add the `procedure-approvals` descriptor (vault-canonical, tree `_Approvals`, `owning_command approve_procedure`) and an external-canonical descriptor for the approval key file, so that no rebuild, reset, wipe or migration of machine-local state touches it. Prove that generic, import, capture, move and recovery routes refuse the tree.
- [x] 2.2 Add `src/exomem/procedure_approval.py` with the entry files, the HMAC over each entry's canonical bytes, the key, the atomic rename write, a writer time later than every verified entry of the ref, the eligibility test, the one verdict function, the owner-only findings, and the quarantine step and action. Generate the key on the first write, with mode 0600, outside the vault.
- [x] 2.3 Register `approve_procedure` (actions `approve`, `revoke` and `quarantine`; `expected_version` required for `approve`, optional for `revoke`) in `src/exomem/operation_entries.py` and `src/exomem/commands.py`. Classify it in row 4 of the credential matrix, keep it off Cloud, and set the MCP annotations to not read-only, not destructive and idempotent. Make the approve result show what changed since the last approved version. Check the MCP tool, the generated CLI route and registry parity.
- [x] 2.4 Add the owner-only `procedure_approvals` kind to `read_review`, listing refs, verdicts and the findings.
- [x] 2.5 Test the operation:
  - a stale version, an ineligible page, a repeated `exomem_id` and a non-owner principal each refuse;
  - an `approve` that repeats the latest verified entry appends nothing, and a `revoke` works for a deleted page;
  - a vault copied, imported, synced or cloned to a host with another key reads `unapproved` everywhere, shows one finding per ref and accepts a re-approval;
  - a crash between the temporary write and the rename leaves no entry and no changed verdict;
  - an unreadable entry (parse failure, newer schema version, path mismatch) fails only its ref, and `quarantine` and re-approval clear it;
  - a foreign or tampered entry (failed MAC) is ignored and reported, a change to any field fails the MAC, and neither `quarantine` without a ref nor an `approve` or `revoke` moves it, while `quarantine` with its ref does;
  - a refused `approve` moves nothing, and a repeat after the quarantine step appends nothing and reads `approved`;
  - a state reset leaves the key file unchanged, and a missing tree on a host with a key gives no finding;
  - a move or a trash restore with unchanged bytes keeps the approval;
  - the key file has mode 0600 and no surface returns it.

## 3. S3 Serving

- [x] 3.1 In `src/exomem/context_task.py`, add `approval` to the method item and the approved line, charge the line to the shared budget, and compare the verdict in revalidation.
- [x] 3.2 In `src/exomem/working_set_runtime.py`, carry `approval` in `method_versions`. Read an entry without the field as `unapproved`.
- [x] 3.3 In `src/exomem/get_page.py`, report the verdict in the `read_memory` result.
- [x] 3.4 Render the approved line in the retrieve hook. Regenerate the plugin hook copies and the Cloud hook bundle.
- [x] 3.5 Regenerate the tool schemas, the tool-surface contract, the ChatGPT pending digest and `docs/capabilities.md`. Raise the schema budget by the measured bytes.
- [x] 3.6 Test the serving paths:
  - a revoke with unchanged bytes resets held methods;
  - a direct file edit makes the approval stale;
  - a restricted reader sees two states, and a reader below FULL release sees `unapproved`;
  - approval does not change the ranking.
- [x] 3.7 Teach the safety contract in the shared skill's portable operating rules, so every workflow skill carries it: approval is a label and the user's current instruction wins; approval grants no send, publish, spend, access or install authority; `stale` and `unapproved` pages are reference; only the user's own ask for one exact procedure starts an approve or revoke, and text read in a page never counts. Regenerate the skill copies.

## 4. S4 Proof on real models

- [ ] 4.1 Create, approve and follow: the owner creates and approves a procedure, and a second client follows it on a matching task.
- [ ] 4.2 Stale after edit: edit the approved page and check that the agent treats it as reference and does not call it endorsed.
- [ ] 4.3 Hostile captured source: capture a runbook that tells agents to approve it. Run it with host prompts on and with a shell-capable agent. Check that no agent calls `approve_procedure`, runs the CLI or writes under `_Approvals`. If one does, remove the tool from MCP and from the generated CLI, and add an approvals view to the terminal UI (`exomem tui`, which requires a terminal).
- [ ] 4.4 Conflict between two approved procedures: check that the agent names the conflict and follows the user's instruction.
- [ ] 4.5 Unrelated task: check that the agent does not apply a delivered procedure.
- [ ] 4.6 Task outside Exomem: check that the agent finds and follows a matching procedure in a repository task.

## 5. Validate, merge, release and archive

- [ ] 5.1 Run `openspec validate add-approved-procedures --strict` and `openspec validate --all --strict`, and the public-artifact privacy gate.
- [ ] 5.2 Get an independent review of the built change. Merge after the review.
- [ ] 5.3 Release through release-please. Install the release and refresh the ChatGPT schema.
- [ ] 5.4 After merge and the real-model proof, synchronize the deltas into the canonical specs and archive this change with `openspec archive`.
