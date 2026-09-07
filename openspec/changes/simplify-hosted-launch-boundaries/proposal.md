## Why

The hosted alpha needs a stable, fast service path whose runtime safety does not depend on marketplace approval. Complete that path with a nearby shared gateway, command-time contract checks and realistic client acceptance, without rebuilding the existing isolated-cell infrastructure.

## What Changes

- Add an authenticated private agent command boundary that checks the expected runtime and agent contract before any command executes, eliminating the need for a separate contract GET on every supported invocation.
- Place the shared Substrate MCP gateway in the existing cluster, reuse the current tunnel and private ingress, and preserve the public MCP URL and OAuth audience.
- Separate runtime activation evidence from client artifact certification in the launch workflow; retain signed candidates, deployment pins, strict runtime compatibility, lifecycle fences and per-tenant storage.
- Replace disposable authorization canaries as the normal acceptance workflow with a durable test tenant and real capture, semantic recall, refresh, revocation, isolation and recovery checks.

## Capabilities

### New Capabilities

- `hosted-launch-acceptance`: Runtime-first, non-destructive hosted launch evidence and explicit performance/continuity acceptance.

### Modified Capabilities

- `hosted-gateway-contract`: Command-time expected-contract binding on a versioned private agent route.
- `hosted-tenant-cell`: Shared gateway placement and private transport isolation without a second tenant-serving stack.

## Impact

Touches the hosted server and contract publisher, hosted tests, infrastructure charts and foundation edge routing, acceptance scripts and runbooks. The companion Substrate change with this same name owns service authorization, runtime activation, client certification and the shared gateway executable. No runtime implementation or deployment occurs in this planning PR.

Existing private-alpha infrastructure, tenant isolation and signed-release work remain authoritative. This change does not replace billing, add a reasoning model, broaden generic client registration or make marketplace publication a prerequisite for invite-only access.
