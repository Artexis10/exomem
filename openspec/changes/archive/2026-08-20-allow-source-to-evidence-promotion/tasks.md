## 1. Regression Tests

- [x] 1.1 Promotion with a reason relocates the file and leaves bytes byte-identical.
- [x] 1.2 Promotion without a reason is refused, and the refusal names the missing reason.
- [x] 1.3 A move out of `Evidence/` is refused even when a reason is supplied.
- [x] 1.4 A move into an append-only tree from a non-append-only location is still refused and still points at `add`/`preserve`.
- [x] 1.5 The product command passes the reason through to the leaf. Added after the first implementation wired only `move_file` and `op_move_file`, leaving `op_manage_memory_file` silently dropping the argument — every promotion arrived reasonless and was refused, so the feature was unreachable from MCP.

## 2. Implementation

- [x] 2.1 Permit `Sources/` → `Evidence/` in the append-only guard when a promotion reason is present.
- [x] 2.2 Record the promotion reason in the activity-log entry.
- [x] 2.3 Rewrite the demotion refusal so it names case completeness instead of repeating inapplicable advice.
- [x] 2.4 Thread `promotion_reason` through `op_move_file` and `op_manage_memory_file`, and regenerate the tool-surface baseline.

## 3. Verification

- [x] 3.1 Run the move-file and vault suites. 82 passed across tier2, schema fidelity, and access.
- [x] 3.2 Run changed-file Ruff. Clean.
- [x] 3.3 Confirm the four boundary tests fail against the unfixed guard (verified by stashing `src/`).

## Follow-up owned outside this change

`scripts/dump-tool-schemas.py` deliberately does not refresh
`deploy/chatgpt/personal-plugin-contract.json`: the connector attestation must be
re-verified against the live external connector first. The MCP tool surface
changed here (one new optional parameter on `manage_memory_file`), so that
attestation needs refreshing before the ChatGPT connector is trusted to match.
