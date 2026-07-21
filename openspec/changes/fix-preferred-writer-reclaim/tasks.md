## 1. Regression Tests

- [x] 1.1 Add a test proving a preferred follower reclaims once the previous holder's lease expires.
- [x] 1.2 Add a test proving repeated reclaim attempts never displace a live holder, and leave its fencing token unchanged.
- [x] 1.3 Add a test proving a non-preferred replica never self-promotes from the renew loop.
- [x] 1.4 Add a test proving the renew loop *invokes* reclaim, not merely that a reclaim helper exists. Verified to fail against the unfixed loop with "renew loop never attempted reclaim while preferred and unleased"; the other three tests pass with the bug restored, so this is the one that actually guards it.

## 2. Implementation

- [x] 2.1 Attempt acquisition from the renew loop when this replica is preferred and holds no fencing token.
- [x] 2.2 Keep reclaim failure non-fatal; a follower unable to acquire is normal steady state, not a fault.

## 3. Verification

- [x] 3.1 Run the writer-lease, mutation-lock, and lease-coordinator suites. 129 passed.
- [x] 3.2 Run changed-file Ruff. Clean.
- [ ] 3.3 Confirm the deployed preferred replica returns to `role: "writer"` after the other holder stops.

## Status of 3.3

**3.3 is deliberately open and is not a code gap.** It can only be exercised
against the running deployment: stop the current holder, then confirm the
preferred replica reports `role: "writer"` on `/health/ready` within roughly one
TTL. The code path it verifies is covered by tests 1.1 and 1.4.

This note exists because an unticked box on a nearly-complete change is exactly
what caused a full misdiagnosis on 2026-07-20 — a reader concluded shipped work
was unshipped. Do not read 3.3 as "the fix was never landed".
