## 1. Contracts

- [x] 1.1 Archive `bound-remote-maintenance` with its deployment evidence, so `remote-maintenance-admission` is canonical.
- [x] 1.2 Write this change and correct the `add-governed-curation-lane` `hosted-gateway-contract` delta, so the remote admission set lives in `remote-maintenance-admission` only; run `openspec validate --all --strict`.

## 2. Job store and runner

- [ ] 2.1 Add `maintenance_jobs.py`: the store in machine-local vault state, registered in `reserved_paths.py`, with single flight, dedupe, owner liveness, `interrupted` marking and retention.
- [ ] 2.2 Add the runner thread, `checkpoint(phase)`, cancellation, the receipt, and the cell memory brake.
- [ ] 2.3 Add the `background_job()` scope in `foreground_priority.py` and honour it in `_foreground_priority_request`.
- [ ] 2.4 Mark dead-owner jobs on service activation and cancel with a bounded join in `_stop_background_workers`.

## 3. Maintenance leaves

- [ ] 3.1 `fix`: compute the plan identity, guard sidecar repair writes, commit in guarded chunks with `skipped_changed`, commit the index refresh last, and call checkpoints.
- [ ] 3.2 `backfill-ids`: compute the plan identity, commit in guarded chunks, record minted IDs, and rebuild references in one short hold.
- [ ] 3.3 `reconcile`: move the census of removed and quarantined state before the first write, list CLIP and frame orphans, compute the plan identity, and call checkpoints between phases.

## 4. Admission and tool surface

- [ ] 4.1 Admit remote `fix`, `backfill-ids` and `reconcile` writes with `plan_id` and `why` as job starts; refuse them without with `MAINTENANCE_PLAN_REQUIRED`; narrow job start and cancel to writer authority.
- [ ] 4.2 Add the `maintenance-job` and `maintenance-job-cancel` modes, their egress adapters and their variants, owner-only.
- [ ] 4.3 Admit remote `collections-store-adopt-local` apply; keep `collections-store-export` apply refused with host remediation.
- [ ] 4.4 Refuse request-bound writes through the legacy `audit_fix` and `reconcile` aliases.

## 5. Guidance and generated surfaces

- [ ] 5.1 Update the scaffold guidance, the `maintain_memory` docstring, the `cli_ops.py` remediation text and the cell memory profile comment.
- [ ] 5.2 Regenerate the schema fixtures and tool-surface digest, the ChatGPT pending digest, the Claude Code plugin, the cloud packages and the capabilities document; raise the tool-schema budget with its reason.

## 6. Verification and delivery

- [ ] 6.1 Prove preview, start, poll, cancel and receipt for `fix`, `backfill-ids`, `reconcile` and adopt-local through an in-process MCP client over a temporary vault, plus one test per boundary refusal.
- [ ] 6.2 Measure run time and boundary hold time of `fix`, `backfill-ids` and `reconcile` on a disposable copy of a real-sized vault and record them on the PR.
- [ ] 6.3 Run Ruff, the privacy gate, the semgrep gate, the scaffold leak test, `scripts/cloud-plugin.py check`, `openspec validate --all --strict` and the full suite.
- [ ] 6.4 Obtain an independent security-lensed review of the exact head, post its note on the PR, and merge after CI passes.
- [ ] 6.5 After the release and the personal service upgrade, preview and run `backfill-ids` remotely on the personal vault, retry the `edit_note` that needed it, and preview `reconcile` remotely.
