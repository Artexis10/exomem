## 1. Implement

- [x] 1.1 Publish the selector registry's values as an `enum` on every MCP selector parameter that has none.
- [x] 1.2 Restrict the MCP door of `activate_context` to model-facing inputs; drop `client`, `session`, `workspace` and `conversation.recent` there.
- [x] 1.3 Update the `activate_context` description and the scaffold echo so they no longer mention earlier turns for remote agents.
- [x] 1.4 Regenerate the MCP schema fixture and the tool surface contract.

## 2. Verify

- [x] 2.1 Dump the Cloud tool list and confirm: every selector has an `enum`; `activate_context` advertises no `recent`, `client`, `session` or `workspace`.
- [x] 2.2 Run the scoped suites: schema fidelity, tool annotations, tool surface contract, conversation acceptance, cloud cell and consolidated tools.
- [x] 2.3 Run `openspec validate --all --strict`.

## 3. Deliver

- [ ] 3.1 Get an independent review, merge, and cut the ordinary release.
- [ ] 3.2 After the operator deploys Cloud and rescans, record the scan result. Draft the manual-review text for the tools that remain combined.
