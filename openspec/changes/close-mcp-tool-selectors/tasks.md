## 1. Implement

- [x] 1.1 Publish the selector registry's values as an `enum` on every MCP selector parameter that has none; fail the server build if a declared `Literal` disagrees with the registry.
- [x] 1.2 Replace the two selector descriptions that repeated the value list with which values write.
- [x] 1.3 Regenerate the MCP schema fixture, the tool surface contract and the pending ChatGPT personal-plugin digest; raise the schema byte budget for the enums.

## 2. Verify

- [x] 2.1 Dump the Cloud tool list and confirm that every registry-covered selector has an `enum` equal to the registry's values.
- [x] 2.2 Run the scoped suites: schema fidelity, schema budget, tool annotations, tool surface contract, connector guardrails, cloud cell and consolidated tools.
- [x] 2.3 Run `openspec validate --all --strict`.

## 3. Deliver

- [ ] 3.1 Get an independent review, merge, and cut the ordinary release.
- [ ] 3.2 After the operator deploys Cloud and rescans, record the scan result.
