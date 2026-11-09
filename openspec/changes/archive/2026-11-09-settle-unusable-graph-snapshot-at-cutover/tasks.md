# Tasks

## 1. Settle a proven-unusable graph snapshot

- [x] 1.1 Add a regression with a real sidecar from the previous schema version; verify that it fails on the unfixed code because `graph_snapshot` stays waiting.
- [x] 1.2 Report a conclusive adoption decline as settled in cutover readiness; verify that promotion records `unproven` and carries no graph handoff.
- [x] 1.3 Run the standby, handoff, catalogue, collections and supervisor suites; compare failures with the unfixed baseline.
- [x] 1.4 Merge the reviewed change, release it, and install it on a managed service whose graph sidecar has an older schema; verify that the upgrade promotes and the graph rebuilds to the current version. Evidence: PR #2495 released in 0.163.0 and installed 2026-11-07 on a managed service whose sidecar had schema 13. Standby adoption declined the unusable snapshot, promotion completed, and the whole-graph rebuild published schema 15. Readiness reported the graph current about one hour after promotion.
