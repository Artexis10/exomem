# Tasks

## 1. Implement exact replica custody

- [x] 1.1 Reuse native bucket authorization and bounded transfer primitives in the declared B2 adapter.
- [x] 1.2 Wire replica-only selection, copy preflight, and deadline-bound verification into the existing store routes.

## 2. Prove the adapter

- [x] 2.1 Verify encrypted replica and restore through the HTTP fixture, including interrupted replies and exact version checks.
- [x] 2.2 Measure authorization and metadata request counts at the existing worker round boundary.
- [ ] 2.3 Rehearse in the designated sandbox bucket and record exact content, range, capabilities, and latency evidence.
