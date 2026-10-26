## 1. Exact-byte intake

- [x] 1.1 Implement session-bound upload tickets, bounded receive, and the CLI handoff route.
- [x] 1.2 Verify caller SHA-256 for text and original file capture, including Cloud handles.
- [x] 1.3 State client limitations and regenerate affected public schemas from their sources.

## 2. Immutable correction

- [x] 2.1 Commit corrected Sources and canonical receipts atomically without changing previous originals.
- [x] 2.2 Resolve current Sources for reads, compilation and original recovery; retain historical access and export.

## 3. Delivery

- [ ] 3.1 Prove the synthetic workflow and negative cases, then complete independent security review and full CI.
- [ ] 3.2 Synchronize specifications, archive this change, merge, and hand publication to the release owner.

The Cloud gateway upload bridge is a separately owned follow-on under the handoff contract in this change.
