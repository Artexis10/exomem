# Proposal

## Why

Existing latency checks measure different boundaries, populations and percentiles, so a passing inner span can conceal a slow public request. One contract will identify those differences and extend the existing watcher without adding another benchmark or acknowledgement framework.

## What Changes

- Define one machine-readable source for latency bars, their owning requirements, measurement boundaries, outcome inclusion, runtime profiles, populations, reference inputs and windows.
- Extend `latency_watch` through existing MCP, REST and activation observations, with explicit coverage, errors, unknown readings and retention loss.
- Add a live warm activation bar: p95 below 1,000 ms across all eligible terminal outcomes at the public activation leaf.
- Preserve packet and CPU behavior; require budgeted read-only activation to return a packet or existing budget abstention within its absolute deadline.
- Reuse the existing recall, conversation, foreground-operation and durable-acknowledgement contracts for their distinct bars.
- Keep unmeasured browse, deep, cold and background-freshness thresholds pending until current measurements support a recorded engineering choice.
- Require attribution before runtime fixes and compare installed before/after arms on the same inputs, with data, authorization and packet identity checks.
- Repair read-only deadline overruns through existing budget and deferral paths, including packet finalization; keep unsafe-to-bound repairs open.
- Keep direct page reads available during optional link recovery. Serve unknown inbound links explicitly instead of building a vault index before releasing the page.
- Keep work that grows with the vault off read requests. Each derived structure's publisher advances it, and a request reads the last published generation. A caller sees its own writes at once, and other changes appear within the publisher's bound. This covers the free-text artifact census, the admitted recall path set, the embedding matrix catch-up, the due-state block, context-pack neighbourhoods, reference resolution and pending visibility.
- Preserve canonical mutation immunity, exact terminal custody and pending-read visibility. Optional heavy paths keep their existing default-off and soft-fail behavior.

## Capabilities

### New Capabilities

- `request-latency-contract`: Identifies latency boundaries and populations across public surfaces, centralizes bars, and reports honest coverage through existing instruments.

### Modified Capabilities

- `agent-bootstrap-contract`: Keep compact and session latency advisories within existing budgets while full and diagnostics profiles retain spans and coverage.
- `attention-queue`: Scope write advice to affected pages while preserving global bootstrap and review counts.
- `get-payload-shape`: Distinguish complete inbound links from unknown inbound links without blocking the permitted page body.
- `live-index-freshness`: Read requests use published derived generations; optional inbound enrichment no longer rebuilds the index on the request thread.
- `release-gate`: The free-text artifact census is published per vault instead of walked per request, and stays exact for service writes.
- `stable-memory-references`: Read-only commands resolve references from the identity projection while an outside edit is pending, with a live identity re-check. Trash-folder owners join the projection in a table used only for references.

Existing latency capabilities retain their numerical bars and lifecycle contracts; this change references them without replacing their requirements.

Ordinary warm public writes additionally target p95 below 1,000 ms, with cold and contended populations reported separately. Existing acknowledgement custody requirements remain in force.

## Impact

Implementation will extend `src/exomem/latency_watch.py`, MCP middleware in `server.py`, the REST command facade in `server_rest.py`, activation observations in `commands.py` and `query_log.py`, and the shared canary for `ask_memory`, `activate_context` and `read_memory`. A packaged `src/exomem/request_latency_contract.json` will hold the bars. Existing gate and runtime paths remain the implementation homes; `accelerate-governed-recall` task 6.1 remains the recall acceptance slice.

The contract adds no MCP tool, model role, paid evaluation, CPU quota increase or live flag switch. Observation remains advisory and cannot block requests or third-party-controlled releases. Existing release and installation ownership applies to product delivery.

The installed observer replaces a checkout dependency. A standard systemd user timer provides scheduling; local status does not establish delivered notifications.
