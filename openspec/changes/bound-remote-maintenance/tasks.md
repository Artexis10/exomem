## 1. Admission Regressions

- [x] 1.1 Add red tests proving MCP, REST, and hosted write-mode maintenance is refused before manager dispatch with stable non-committed remediation.
- [x] 1.2 Add regressions proving remote audit/dry runs and CLI/direct-Python write maintenance still reach ordinary dispatch.

## 2. Common Guard And Guidance

- [x] 2.1 Implement one surface-aware guard in the common dispatcher and make the admission regressions pass.
- [x] 2.2 Update the `maintain_memory` tool description, generated schema contract, and generic scaffold guidance; verify schema fidelity and leak checks.

## 3. Verification And Delivery

- [x] 3.1 Run focused command-surface tests, Ruff, privacy validation, and strict OpenSpec validation.
- [x] 3.2 Obtain independent adversarial review and address every actionable finding.
- [ ] 3.3 Merge, publish a patch release, deploy both personal and POLLY cells, then verify readiness, recall, immediate remote refusal, and a free mutation boundary.
