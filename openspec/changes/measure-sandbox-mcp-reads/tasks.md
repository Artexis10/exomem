# Tasks

## 1. Observer foundation

- [x] 1.1 Extend canonical and collection read cases; prove mutation and malformed-operation rejection before connection with the FastMCP suite.
- [x] 1.2 Select configured bars per case; verify thresholds, minimums, default sample counts, and invalid selections before connection.
- [x] 1.3 Stop timing at the parsed terminal; verify assertion cost stays outside elapsed time and failures retain timing.
- [x] 1.4 Report descriptive p50, p95, and terminal counts; verify failed and unattempted cases preserve honest evidence.
- [x] 1.5 Decide bar coverage with one `LatencyBar.covers(tool, deep)` predicate; verify with the FastMCP suite that typed tools report their own tool and that a `deep` mismatch fails before connecting.
- [x] 1.6 Expose `measure_call` and `load_cases` with an optional case `group`; verify a runner times a grouped case and that the canary report is unchanged.
- [x] 1.7 Add the `mcp-read-paired` reference and a pending worker-socket bar under `request-latency-v5`; verify earlier rows are unchanged and the bar never labels live traffic.

## 2. Frozen sandbox and source truth

- [ ] 2.1 Freeze isolated sandbox data and runtime identities; verify copied files and stored rows with counts and source spot checks.
- [ ] 2.2 Author independent source truth for all nine operation families; verify identities and material without using measured outputs as keys.
- [ ] 2.3 Prove sandbox isolation and blocked external effects; retain reproducible setup and cleanup evidence outside the repository.

## 3. Matched measurement and diagnosis

- [ ] 3.1 Run matched cases on frozen arms; report per-case differences and confidence intervals with unchanged source keys.
- [ ] 3.2 Attribute transport, authentication, governance, egress, rendering, store, and serialization costs; report unavailable layers explicitly.
- [ ] 3.3 Establish the paired allowances from matched runs of identical code; verify the private sandbox reaches the configured p95 target of 100 ms for the six collection-store groups.
- [ ] 3.4 Repair the largest measured costs without weakening governance; verify each correction through the same instrument and boundary.

## 4. CI and delivery

- [ ] 4.1 Run the shared observer as a CI regression gate with neutral fixtures and configured budgets; verify failed and incomplete exits.
- [ ] 4.2 Complete independent adversarial review and integrated checks; record product-versus-proof weight and preserve all governance guarantees.
- [ ] 4.3 Merge and release through the existing release owner; verify the released package and retain matched before-and-after evidence.
- [ ] 4.4 Synchronize the existing latency capability and this delta when their tasks are shipped; validate and archive without losing later requirements.
