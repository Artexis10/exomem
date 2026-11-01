## Context

`LeaseManager._run_absorbing_capture_contention` waits out a busy boundary for a write that has an idempotency key. The key is what makes a re-attempt safe: a busy refusal is raised before the commit and releases the key's claim, and an attempt that observed a commit is never run again. `_effective_idempotency_key` already derives an implicit key from a retry scope plus the command digest (`implicit:<cell or vault>[\0client:v1:<binding>]\0<scope>\0<digest>`, 600 s). MCP passes `mcp_retry_scope()` as that scope. REST and the CLI pass nothing.

## Decisions

### D1. Each surface supplies its own retry scope; the key function gains no fallback

- REST passes no implicit scope. A digest-derived identity would replay a write the client repeats on purpose: the Studio re-sends a selection after toggling and re-runs a plan after changing the selection, and review reproduced the server keeping the old selection and serving a stale plan. A keyless REST client that waited and then timed out could commit twice, so it keeps the immediate refusal. The Studio sends a fresh key per action, so its writes take the keyed wait and stay distinct.
- The CLI and terminal UI wait out contention with no replay identity at all. An in-process caller cannot lose a reply, so a receipt buys nothing; a re-attempt is safe because commit observation (`commit_state`) stops any attempt that committed from running again. A per-invocation key was rejected in review: it passed the vocabulary-application gate, which requires an identity a retry can reuse, and a failed attempt then left the reviewed decision bound to an identity nobody could present again. It would also have made every CLI write depend on a healthy replay store.
- `_effective_idempotency_key` keeps returning no key when a caller supplies neither a key nor a scope. Credential-issuing commands clear both before key derivation so that a bearer never enters the replay store; a fallback inside the key function would bypass that rule for every internal caller.

Rejected: a key the server mints for a keyless remote caller with no stable scope. Such a client can give up during the wait and resend; both attempts then commit.

### D2. The cross-process waiter signal is a shared `flock` on a sidecar

A process whose thread fails its first try for the boundary takes `LOCK_SH | LOCK_NB` on `<digest>.waiters.lock` beside the boundary's lock file, retrying on later polls when a probe holds the file, and drops it when that thread stops waiting. Only the polling thread, which holds the process's boundary guard, raises the signal, so the cost falls only on a contended acquire. `waiter_count()` returns the local count when it is positive. Otherwise it tries `LOCK_EX | LOCK_NB` on its own descriptor of the same file, at most once per poll interval, and reports one waiter when that fails. The kernel drops the shared lock when the waiter's process exits, so a crashed waiter leaves no stale signal.

Windows has no shared advisory lock in `msvcrt`, so there the signal stays process-local, as today.

Rejected: marker files per waiter. A crashed process leaves its marker behind, and a liveness check by process id is unreliable under id reuse.

### D3. Upload commits reuse the keyed-write wait

The direct `/upload` handlers acquire the boundary before they read or write any vault byte, so a busy acquire has had no effect. They wait out ordinary contention through the same queue, waiter cap and refusal causes as keyed writes. The tus commit already runs keyed under the session id and so already waits; when it still ends `retryable` with a busy or warming code, the server schedules its own re-attempt with backoff until the session expires.

### D4. A moved navigation auxiliary is a retryable busy, and background commits take no slot

Navigation auxiliaries are planned with content guards before the narrow boundary is taken, so a writer outside the line can change one while a write waits. After a refused batch, with the boundary still held, the commit seams recheck every guard of the write's own basis: primary page, read dependencies, source closure, vocabulary, lifecycle and retained input. The seam raises `MUTATION_BUSY` with `committed: false` and `cause: auxiliary_changed` only on positive evidence: no basis guard changed and an auxiliary's guard no longer holds. The absorption loop already re-runs such a refusal. In every other case the old error stays, so a refusal with no moved guard (a full disk, a read-only folder, a residue limit, an existing path variant) is never re-run. The same check stops a failed closure guard from being reported as `STALE_VOCABULARY_BINDING`, and a failed source-closure guard on the creation path reports `STALE_SEMANTIC_WRITE` like the other two seams.

A session commit runs on a background thread, not an anyio request worker. It still waits in line, but it neither takes nor counts a waiter slot, so it cannot make a request's write refuse as full.

Rejected: re-planning inside each leaf, which needs a loop in about ten leaves, and planning under the boundary, which lengthens the critical section and touches about eighteen planners.

## Risks

- REST callers without a key gain nothing from this change; the deployment guide tells them to send one key per intended write.
- A waiter process that is suspended mid-wait (for example by SIGSTOP) keeps its signal until it resumes or exits. Derived rows and store ticks still advance one unit per pass; graph republication defers until then.
- A foreign waiter makes background work step aside more often. Each unit still completes before the holder checks, so background work keeps making progress.
