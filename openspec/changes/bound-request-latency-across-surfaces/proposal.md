# Proposal

## Why

Existing latency checks measure different boundaries, populations and percentiles, so a passing inner span can conceal a slow public request. One contract will identify those differences and extend the existing watcher without adding another benchmark or acknowledgement framework.

## What Changes

- Define one machine-readable source for latency bars, their owning requirements, measurement boundaries, runtime profiles, populations, reference inputs and windows.
- Extend `latency_watch` through existing MCP, REST and activation observations, with explicit coverage, errors, unknown readings and retention loss.
- Add a live warm activation bar: p95 below 1,000 ms at the public activation leaf, with packet and CPU behavior preserved.
- Reuse the existing recall, conversation, foreground-operation and durable-acknowledgement contracts for their distinct bars.
- Keep unmeasured read, browse, deep, cold and background-freshness thresholds pending until current measurements support a recorded engineering choice.
- Require attribution before runtime fixes and compare installed before/after arms on the same inputs, with data, authorization and packet identity checks.
- Preserve cooperative deadlines, canonical commit immunity, exact terminal custody and pending-read visibility. Optional heavy paths keep their existing default-off and soft-fail behavior.

## Capabilities

### New Capabilities

- `request-latency-contract`: Identifies latency boundaries and populations across public surfaces, centralizes bars, and reports honest coverage through existing instruments.

### Modified Capabilities

None. Existing capabilities retain their numerical bars and lifecycle contracts; this change references them without replacing their requirements.

## Impact

Implementation will extend `src/exomem/latency_watch.py`, MCP middleware in `server.py`, the REST command facade in `server_rest.py`, and activation observations in `commands.py` and `query_log.py`. A packaged `src/exomem/request_latency_contract.json` will hold the bars. Existing gate and runtime paths remain the implementation homes; `accelerate-governed-recall` task 6.1 remains the recall acceptance slice.

The contract adds no tool, model role, paid evaluation, CPU quota increase or live flag switch. Observation remains advisory and cannot block requests or third-party-controlled releases. Existing release and installation ownership applies to product delivery.
