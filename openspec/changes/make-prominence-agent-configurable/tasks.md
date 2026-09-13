## 1. Preference storage and resolution

- [x] 1.1 Add failing tests for all four saved levels, principal/vault isolation, missing identity, invalid input, stale revisions, corrupt state and operator overrides; run the new focused module and record the expected failures.
- [x] 1.2 Implement bounded atomic preference storage and request-scoped resolution; pass the new module and existing prominence tests without modifying live settings.

## 2. Canonical client control

- [x] 2.1 Register configure_memory inspect/set across MCP, REST and CLI with read/write, identity, egress and receipt coverage; verify registry and real adapter tests in temporary state.
- [x] 2.2 Bind current vault/client context and use the saved choice in bootstrap, workflow capture and envelope projections; test two principals, two vaults and known/unknown client defaults.
- [x] 2.3 Teach the scope and inspect/set route in bootstrap and public guidance, preserving legacy local hook behavior; regenerate canonical tool metadata and pass surface/schema/privacy checks.

## 3. Delivery

- [x] 3.1 Obtain an independent security/correctness review of the preference boundary and address its findings; verify the reviewed scenarios.
- [ ] 3.2 Run completion checks and strict OpenSpec validation, commit and push the task branch, and open a ready PR with scoped test evidence.
- [ ] 3.3 Deliver under the standing release authority, verify selection through the deployed connector without changing an unrelated cell, and synchronize/archive this change once shipped.
