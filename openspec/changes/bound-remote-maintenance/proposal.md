## Why

A request-bound MCP, REST, or hosted call can start write-mode vault maintenance,
return or be abandoned at the transport deadline, and leave its synchronous worker
running with the mutation boundary held for tens of minutes. One maintenance call
can therefore make every other ChatGPT or agent session fail with `MUTATION_BUSY`.

## What Changes

- Refuse write-mode `maintain_memory` invocations on request-bound remote surfaces
  before writer-manager or mutation-boundary admission.
- Keep read-only audit and every explicit dry run available remotely.
- Keep actual repair and reconcile available through the local operator CLI.
- Return a stable `MAINTENANCE_REQUIRES_CLI` error with actionable remediation and
  teach the generated tool contract where write maintenance belongs.

## Capabilities

### New Capabilities

- `remote-maintenance-admission`: Surface-aware admission for bounded remote
  maintenance and local operator repair.

### Modified Capabilities

None.

## Impact

The common command dispatcher, `maintain_memory` tool description, MCP schema
fixture and generic installed skill guidance change. MCP, REST, and hosted clients
that previously requested real maintenance writes receive an immediate structured
refusal; CLI/operator behavior and all read-only maintenance behavior stay intact.
No dependency, Markdown, sidecar-schema, or model behavior changes.
