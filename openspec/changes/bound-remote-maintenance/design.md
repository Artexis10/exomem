## Context

All product surfaces converge on `writer_lease.invoke_command`, and each adapter
binds a trusted `ActiveSurfaceDescriptor` around dispatch. The common dispatcher
already classifies mixed-mode commands before it reaches the writer manager. This
is the last shared point where remote write maintenance can be refused without
claiming authority or duplicating policy in MCP, REST, and hosted adapters.

`maintain_memory(mode="reconcile")` currently owns a broad outer mutation boundary.
That serialization is required by write seams inside reconcile. Removing it as an
incident patch would permit unsafe overlap, while transport cancellation cannot
reliably stop an already-running synchronous worker.

## Goals / Non-Goals

**Goals:**

- Make abandoned request-bound maintenance unable to block other vault clients.
- Preserve one canonical maintenance implementation and one admission policy.
- Preserve remote audit/preview and local operator repair.

**Non-Goals:**

- Convert maintenance into a durable background-job protocol.
- Narrow reconcile's internal mutation critical sections.
- Repair graph stabilization or production-vault indexing performance in this change.

## Decisions

### Refuse at the common dispatcher

After invocation read/write classification and before `get_manager()`, dispatch
checks the active surface. A write-mode `maintain_memory` call is refused for the
known request-bound surfaces `mcp`, `rest`, and `hosted`. CLI and descriptor-free
direct Python calls retain current behavior.

Adapter-local checks were rejected because they would duplicate policy and leave
future routes vulnerable. Removing maintenance from the mutation boundary was
rejected because reconcile's existing write seams depend on that serialization.

### Classify behavior, not mode names

The guard consumes the result of the existing `invocation_is_read_only` registry
classification. Audit, default-safe fix/backfill previews, and explicit reconcile
dry runs remain available without a second selector table that could drift.

### Return a terminal operation refusal

The guard raises `OpError` with stable code `MAINTENANCE_REQUIRES_CLI`, explicit
operator commands, and terminal/non-committed details. Generated MCP wrappers and
the REST/hosted envelope paths already project this error consistently.

## Risks / Trade-offs

- [Remote agents lose direct write repair] -> Keep audit and dry-run diagnosis
  available and provide exact operator CLI remediation.
- [A future remote surface misses the explicit set] -> Pin known descriptors in
  dispatcher tests and require new request-bound adapters to extend admission coverage.
- [Local maintenance can still be slow] -> Keep it under operator control; durable
  background jobs and reconcile performance remain explicit follow-up work.

## Migration Plan

Deploy as a patch release to both cells, restart each service, and verify readiness,
read recall, remote dry-run availability, and immediate remote write refusal without a
held mutation boundary. Rollback is a package downgrade and service restart; it
restores prior behavior but also restores the outage risk.
