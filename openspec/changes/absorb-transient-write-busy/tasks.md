## 1. Retry identity for REST and in-process writes

- [ ] 1.1 REST: when no `Idempotency-Key` header arrives, pass an implicit retry scope from the resolved principal's canonical audience to `invoke_command`; pass none for an unresolved or most-restrictive principal.
- [ ] 1.2 CLI and terminal UI (after PR #2250, with task 3): when no explicit key is set, wait out ordinary contention inside the invocation through `_absorb_contention` without a replay receipt; commit observation (`commit_state`) keeps an attempt that committed from running again. A per-invocation implicit key was rejected in review: it passed the vocabulary-application identity gate with an identity no retry can reproduce.
- [ ] 1.3 Tests: an unkeyed REST write waits out a held boundary and commits once; a REST resend after a dropped reply replays without a second commit; two REST principals do not share a result; a credential-issuing command keeps no implicit identity. With 1.2: an unkeyed CLI write waits out a held boundary, leaves no receipt, and a second identical invocation commits again; an unkeyed CLI vocabulary application is still refused.

## 2. Cross-process waiter signal

- [ ] 2.1 `mutation_lock`: hold a shared advisory lock on the boundary's waiter sidecar while any thread of the process waits; `waiter_count()` reports a waiter in another process; Windows stays process-local.
- [ ] 2.2 Tests: a holder sees a waiter in a second process and stops seeing it after that process stops waiting or is killed; a background unit loop steps aside for the foreign waiter.

## 3. Upload commits wait out contention (after PR #2239 merges)

- [ ] 3.1 Route the direct `/upload` commit acquisitions through the keyed-write wait, with its queue, waiter cap and refusal causes.
- [ ] 3.2 Re-attempt a tus session commit that ends retryable with a busy or warming code, with backoff, until the session expires.
- [ ] 3.3 Tests: a direct upload and a tus upload each commit once while another holder keeps the boundary for less than the wait budget.

## 4. Proof and closure

- [ ] 4.1 On a disposable vault copy with its own state root, a second process holds the boundary for 4 s while clients write over MCP, REST, `/upload` and tus: no busy refusal, every write committed once, and a blind unkeyed REST resend after a dropped reply attaches.
- [ ] 4.2 After release, the caller-visible refusal journal shows no busy refusals caused by ordinary contention on the personal service.
- [ ] 4.3 Sync the delta into `openspec/specs/hosted-mutation-safety/spec.md`, archive with `openspec archive absorb-transient-write-busy`, and run `openspec validate --all --strict` before and after.
