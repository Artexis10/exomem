## Why

The failover-safe connector promises single-writer safety via an opt-in writer lease, but the lease is validated only ONCE at the start of a mutating command and is never re-checked or passed down into the write itself. A replica that loses or outlives its lease — it expires, and another replica acquires a newer fencing token — can still complete an in-flight vault mutation, because the vault writers never consult the fencing token. This defeats the single-writer guarantee under exactly the failover it was built for. (Audit finding CDX-06, HIGH-plausible; needs concurrency/multi-replica to trigger.)

Evidence: `src/exomem/writer_lease.py` `invoke` (~289-300) validates the lease then calls the mutation without threading or re-validating the fencing token; vault writers (`vault.batch_atomic_write`) never check it; the renewer only clears local state on a rejected renewal (~345-354) and cannot cancel an already-running command.

## What Changes

- Thread the acquired fencing token into the mutating write path.
- Re-validate the fencing token (compare-and-swap against the coordinator's current token) at the atomic-write boundary, so a stale token aborts the write with a fenced error instead of committing.
- Make lease loss during an in-flight command fail closed: the write does not land if the holder was superseded.
- Preserve current behavior when the lease is disabled (no `EXOMEM_WRITER_LEASE_URL`) and for read-only commands.

## Capabilities

### New Capabilities

- `writer-lease-fencing`: Guarantees that a vault mutation completes only while its issuing replica still holds the lease it was authorized under, so a superseded replica cannot land a stale write during failover.

## Impact

Affects `src/exomem/writer_lease.py` and the atomic write boundary in `src/exomem/vault.py`; no change to the read path, to lease-disabled operation, or to the MCP tool schema. Existing writer-lease tests must stay green; a new concurrency test covers the fenced-write case.
