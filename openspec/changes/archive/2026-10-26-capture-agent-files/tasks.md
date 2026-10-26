## 1. Exact-byte intake

- [x] 1.1 Implement session-bound upload tickets, bounded receive, and the CLI handoff route.
- [x] 1.2 Verify caller SHA-256 for text and original file capture, including Cloud handles.
- [x] 1.3 State client limitations and regenerate affected public schemas from their sources.

## 2. Immutable correction

- [x] 2.1 Commit corrected Sources and canonical receipts atomically without changing previous originals.
- [x] 2.2 Resolve current Sources for reads, compilation and original recovery; retain historical access and export.

## 3. Delivery

- [x] 3.1 Prove the synthetic workflow and negative cases, then complete independent security review and full CI.
- [x] 3.2 Synchronize specifications, archive this change, merge, and hand publication to the release owner.

The Cloud gateway upload bridge is a separately owned follow-on under the handoff contract in this change.

## Delivery evidence

The implementation merged in [#2132](https://github.com/Artexis10/exomem/pull/2132) at `c333eb423f1daa96508f6ca45414c7b88470542c`.
[Independent review](https://github.com/Artexis10/exomem/pull/2132#issuecomment-6424307359) approved head `88c01d9f95361f2f64660323d00a1cca261b0f17` with zero blocking findings.
[Full CI](https://github.com/Artexis10/exomem/actions/runs/40378203118) passed on that head.
Independent verification covered the published MCP handoff, real CLI and HTTP upload, Source capture, and exact original recovery.
The verifier checked UTF-16 bytes, original filename and MIME, current and historical reads, and withheld successors.

Canonical specifications include both deltas, and `openspec archive` closed this change.
The release owner accepted publication for the next eligible release. Package publication, connector discovery and deployed upload reachability remain unverified.
The Cloud gateway upload bridge remains a separately owned follow-on under the canonical handoff contract.
