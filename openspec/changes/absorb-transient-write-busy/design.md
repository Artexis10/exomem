## Context

`LeaseManager._run_absorbing_capture_contention` waits out a busy boundary for a write that has an idempotency key. The key is what makes a re-attempt safe: a busy refusal is raised before the commit and releases the key's claim, and an attempt that observed a commit is never run again. `_effective_idempotency_key` already derives an implicit key from a retry scope plus the command digest (`implicit:<cell or vault>[\0client:v1:<binding>]\0<scope>\0<digest>`, 600 s). MCP passes `mcp_retry_scope()` as that scope. REST and the CLI pass nothing.

## Decisions

### D1. Each surface supplies its own retry scope; the key function gains no fallback

- REST passes the authenticated principal's scope as the implicit scope when no `Idempotency-Key` header arrives. The principal is stable across a resend with the same credential, so a resend after a lost reply attaches to the first attempt.
- The CLI and terminal UI pass `invocation:<uuid4>`, minted once per call. An in-process caller cannot lose a reply, so cross-call replay buys nothing and would change what an intentional repeat does.
- `_effective_idempotency_key` keeps returning no key when a caller supplies neither a key nor a scope. Credential-issuing commands clear both before key derivation so that a bearer never enters the replay store; a fallback inside the key function would bypass that rule for every internal caller.

Rejected: a random per-request key for remote callers with no stable scope. Such a client can give up during the wait and resend; both attempts then commit.

### D2. The cross-process waiter signal is a shared `flock` on a sidecar

While at least one thread of a process waits inside `hold()`, the process holds `LOCK_SH` on `<digest>.waiters.lock` beside the boundary's lock file. `waiter_count()` returns the local count when it is positive. Otherwise it tries `LOCK_EX | LOCK_NB` on its own descriptor of the same file and reports one waiter when that fails. The kernel drops the shared lock when the waiter's process exits, so a crashed waiter leaves no stale signal. The shared lock is taken blocking: a probe holds its exclusive lock only for the try and the release.

Windows has no shared advisory lock in `msvcrt`, so there the signal stays process-local, as today.

Rejected: marker files per waiter. A crashed process leaves its marker behind, and a liveness check by process id is unreliable under id reuse.

### D3. Upload commits reuse the keyed-write wait

The direct `/upload` handlers acquire the boundary before they read or write any vault byte, so a busy acquire has had no effect. They wait out ordinary contention through the same queue, waiter cap and refusal causes as keyed writes. The tus commit already runs keyed under the session id and so already waits; when it still ends `retryable` with a busy or warming code, the server schedules its own re-attempt with backoff until the session expires.

## Risks

- An identical unkeyed REST write from the same principal inside 600 s now replays instead of committing twice. MCP already behaves this way; an explicit `Idempotency-Key` per intended write keeps both.
- A foreign waiter makes background work step aside more often. Each unit still completes before the holder checks, so background work keeps making progress.
