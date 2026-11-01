## 1. Retry identity for Studio and in-process writes

- [x] 1.1 Studio: send a distinct `Idempotency-Key` with every action from `studio/api.v1.js`; REST without a key stays unchanged (no implicit scope). Delivered in #2255 (31e1627b7).
- [x] 1.2 CLI and terminal UI (after PR #2250, with task 3): when no explicit key is set, wait out ordinary contention inside the invocation through `_absorb_contention` without a replay receipt; commit observation (`commit_state`) keeps an attempt that committed from running again. A per-invocation implicit key was rejected in review: it passed the vocabulary-application identity gate with an identity no retry can reproduce. Delivered with task 3 in the follow-up PR (absorb_unkeyed from invoke_prepared).
- [x] 1.3 Tests: the existing Studio flow and REST suites still pass with per-action keys; a credential-issuing command keeps no implicit identity. With 1.2: an unkeyed CLI write waits out a held boundary, leaves no receipt, and a second identical invocation commits again; an unkeyed CLI vocabulary application is still refused. REST parts in #2255; CLI parts in the follow-up PR.

## 2. Cross-process waiter signal

- [x] 2.1 `mutation_lock`: hold a shared advisory lock on the boundary's waiter sidecar from a thread's first failed try until it stops waiting; `waiter_count()` reports a waiter in another process; Windows stays process-local. Delivered in #2255 (31e1627b7).
- [x] 2.2 Tests: a holder sees a waiter in a second process and stops seeing it after that process stops waiting or is killed; a background unit loop steps aside for the foreign waiter. Delivered in #2255 (31e1627b7); the consumer-level step-aside is covered by the unchanged consumers plus the mutation_lock cross-process tests.

## 3. Upload commits wait out contention (after PR #2239 merges)

- [x] 3.1 Route the direct `/upload` commit acquisitions through the keyed-write wait, with its queue, waiter cap and refusal causes. Follow-up PR: absorbing_mutation_guard on every /upload guard.
- [x] 3.2 Re-attempt a tus session commit that ends retryable with a busy or warming code, with backoff, until the session expires. Follow-up PR: one re-attempt timer per session, 2 s doubling to 60 s.
- [x] 3.3 Tests: a direct upload and a tus upload each commit once while another holder keeps the boundary for less than the wait budget. Follow-up PR: held-boundary /upload and tus tests.
- [x] 3.4 Route the source capture guards of client artifacts (`capture_source`, `preserve_artifacts`) through the same wait. Follow-up PR: absorbing guard on source captures; the boundary hold still scales with artifact size and is a separate change.

## 4. Proof and closure

- [ ] 4.1 On a disposable vault copy with its own state root, a second process holds the boundary for 4 s while clients write over MCP, REST, `/upload` and tus: no busy refusal, every write committed once, and a blind unkeyed REST resend after a dropped reply attaches.
- [ ] 4.2 After release, the caller-visible refusal journal shows no busy refusals caused by ordinary contention on the personal service.
- [ ] 4.3 Sync the delta into `openspec/specs/hosted-mutation-safety/spec.md`, archive with `openspec archive absorb-transient-write-busy`, and run `openspec validate --all --strict` before and after.
