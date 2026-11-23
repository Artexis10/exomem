# Tasks

## 1. Relay

- [x] 1.1 Parse and validate the notice header in cloud mode; verify valid, oversized, malformed, wrong-version and non-cloud cases.
- [x] 1.2 Add `service_notice` to object results in the `bind_vault` wrapper after the egress filter and on the refusal path; verify recall, save and refusal results through the cloud MCP surface.

## 2. Delivery

- [ ] 2.1 Review the change independently; resolve findings; pass scoped tests and CI.
- [ ] 2.2 Merge, and hand the cell image to the Exomem Cloud release owner for the next release.
