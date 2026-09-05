## 1. Configuration and wiring

- [x] 1.1 Add failing real-configuration tests for explicit v3/v4 selection, unknown profiles, incompatible readers, and legacy defaults; retain the failing output.
- [x] 1.2 Carry the trusted runtime target profile through provisioner chart values and the StatefulSet environment into validated runtime selection; verify the focused configuration, chart, and provisioner tests pass.

## 2. Integration evidence

- [x] 2.1 Exercise authenticated v4 contract discovery from rendered environment using the production configuration class, rejecting unselected profiles and preserving lifecycle action restrictions; verify regression and protected-tree suites pass without overriding selection.
- [x] 2.2 Independently review the actual diff and reproduce the failure/fix; run scoped gates, the completion test corpus, public-artifact privacy validation, and strict OpenSpec validation before opening a ready pull request.

## 3. Delivery

- [ ] 3.1 Merge the reviewed change when authorized, rebuild and compose runtime/provisioner candidates, and verify the deployed cell returns the selected authenticated profile contract before binding or promotion.
- [ ] 3.2 Synchronize and archive the shipped change through OpenSpec, validating all specs strictly before and after archive.
