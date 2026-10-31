Retirement scope (2026-10-11): Retirement supersedes the Hosted adapter cases. MCP/REST maintenance admission and every surviving unchecked task remain in scope.

## 1. Admission Regressions

- [x] 1.1 Add red tests proving MCP, REST, and hosted write-mode maintenance is refused before manager dispatch with stable non-committed remediation.
- [x] 1.2 Add regressions proving remote audit/dry runs and CLI/direct-Python write maintenance still reach ordinary dispatch.

## 2. Common Guard And Guidance

- [x] 2.1 Implement one surface-aware guard in the common dispatcher and make the admission regressions pass.
- [x] 2.2 Update the `maintain_memory` tool description, generated schema contract, and generic scaffold guidance; verify schema fidelity and leak checks.

## 3. Verification And Delivery

- [x] 3.1 Run focused command-surface tests, Ruff, privacy validation, and strict OpenSpec validation.
- [x] 3.2 Obtain independent adversarial review and address every actionable finding.
- [x] 3.3 Merge, publish a patch release, deploy both the personal cell and the client cell, then verify readiness, recall, immediate remote refusal, and a free mutation boundary.
  Evidence (2026-10-30): #797 (`d7dc55ce8`) merged and first shipped in v0.62.0; the personal service and the client cell both report a version later than v0.62.0 on `/health`; the Hosted adapter cases were retired by #1691. `add-remote-maintenance-jobs` supersedes the refusal with governed jobs.
