# Tasks

## 1. Bound audit preparation and checks

- [x] 1.1 Add focused regressions for interrupted corpus preparation and aggregate checks; verify incomplete input never produces an absence finding.
- [x] 1.2 Reuse the request budget in `op_audit`, `audit.audit` and expensive read helpers; verify bounded checkpoints stop parsing, hashing and fallback work.
- [x] 1.3 Carry interruption through resolver proof and shared corpus flights; verify no fallback restart, cancelled foreign owner or poisoned waiter.
- [x] 1.4 Preserve complete-category findings and public coverage in both detail modes; verify semantic and legacy flags cannot imply missing work finished.
- [x] 1.5 Document partial audit coverage beside the existing audit contract; verify examples distinguish unknown coverage from a clean audit.

## 2. Bound fresh result authorization

- [x] 2.1 Add regressions for direct and default audit routes, ordinary attention and unbudgeted callers; verify only budgeted audited reads select the new path.
- [x] 2.2 Carry optional checkpoints through writer filters and accepted store read batches; verify the expired request stops its accepted read work.
- [x] 2.3 Preserve fresh admission after concurrent withholding; verify no findings, paths or aggregate counts escape an interrupted authorization pass.
- [x] 2.4 Add the fixed internal budget terminal; verify its complete delivery with active governance and a cold census performs no corpus enumeration.
- [x] 2.5 Document the budget error and narrower-category recovery; verify ordinary errors retain their existing filtering and callers cannot select the fixed carrier.

## 3. Verify and deliver the integrated repair

- [x] 3.1 Run scoped audit, budget and egress suites, then required completion checks; compare failures with the unchanged baseline.
- [x] 3.2 Complete independent adversarial review of the exact batch; record C4, C6, product/proof weight and the ship decision.
- [x] 3.3 Run a bounded connector workflow in isolated state; verify incomplete coverage and authorization errors remain explicit to the calling agent.
- [x] 3.4 Merge the reviewed green batch and publish its ordinary release; verify the release contains the reviewed commit. Evidence: PR #2425 merged as bf2138070; v0.162.0 and later contain it.
- [x] 3.5 Within an approved personal maintenance window, promote the containing release and run one real audit; verify its client terminal and server budget evidence. Evidence: one default audit on 0.163.0 (2026-11-07) returned normally with the 50 s budget applied, 2 of 32 categories complete, the rest truncated or skipped with coverage unknown, and no findings claimed. The server log recorded the audit call at 45.1 s. On 0.167.0 (2026-11-09), a narrowed audit of one category completed with coverage complete; an earlier attempt under write load reported that category skipped with coverage unknown.
- [x] 3.6 Update the incident evidence and synchronize/archive this change only after its required live acceptance passes; verify strict OpenSpec validation. Evidence: the live acceptance above is recorded in the operator's incident records; this archive syncs the change, with `openspec validate --all --strict` passing before and after.
