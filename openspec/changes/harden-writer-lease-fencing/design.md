## Context

The lease flow today: `writer_lease.invoke(command)` → if the command is read-only or the lease is disabled, run directly; otherwise `ensure_writer()` (acquire/verify holder) once, then run the mutation. The fencing token returned by acquire/renew is stored in local lease state but never reaches the write. So the window between "lease verified" and "bytes hit disk" is unguarded: TTL can expire and replica B can take the next token while replica A's command is still executing.

## Goals / Non-Goals

- Goal: a mutation lands only if the issuing replica's fencing token is still current at the moment of commit.
- Goal: fail closed — a stale token aborts with a clear fenced error; no partial write.
- Non-goal: distributed transactions or cross-replica locking beyond the existing coordinator's fencing-token semantics.
- Non-goal: changing lease-disabled or read-only behavior, or the MCP tool surface.

## Approach (to be finalized by the implementer)

1. Capture the fencing token from `ensure_writer()` and pass it through `invoke` into the command's write path (an explicit parameter or a contextvar scoped to the command).
2. At the `batch_atomic_write` commit boundary — after staging, before/around `os.replace` — re-validate the token: cheap local check against the last-known-good token, and/or a coordinator check-and-fence call if the coordinator contract supports it. Prefer the strongest check the coordinator already offers to avoid a new round-trip per write where possible.
3. If the token is stale (renewal was rejected, or the coordinator reports a newer holder), raise a `WRITER_FENCED` error and DO NOT replace staged temps (clean them up).
4. Ensure the renewer path marks the token stale synchronously enough that an in-flight command sees it.

Open questions for the implementer: exact coordinator contract for a check-and-fence vs. relying on the renewer's local staleness flag; whether to re-validate once per batch or per file (batch is likely sufficient given atomic staging).

## Risks

- A per-write coordinator round-trip could add latency; prefer a local staleness flag updated by the renewer, with a coordinator confirmation only on suspicion. Measure against the latency gate.
- Must not introduce a deadlock/livelock between the renewer thread and the write path.
