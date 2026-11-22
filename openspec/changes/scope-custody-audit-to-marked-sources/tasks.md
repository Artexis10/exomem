## 1. Custody-only corpus read

- [x] 1.1 Give `_parse_all` a selector that runs before admission; for a selection of exactly `{custody_source}`, keep only corpus pages whose frontmatter records `custody_only: true`, and keep any page whose frontmatter it cannot decide, in file and store-served vaults.
- [x] 1.2 Pass the custody check an on-demand lookup that admits and parses one named path, caches it for the call, checks the request budget, and returns `None` where the full parse would have no page.
- [x] 1.3 Leave every other selection on its existing read.

## 2. Proof

- [x] 2.1 Tests: a compiled custody Source gives no finding; an uncompiled one gives one; a hidden derivative reports missing with nothing disclosed; a predecessor reached through `supersedes` counts; unmarked pages are never admitted or parsed; a mixed selection still parses the corpus; a budget that expires in the lookup reports `unknown`.
- [x] 2.2 Equivalence and timing on a large vault snapshot: the custody-only findings equal those of the full-corpus check, coverage is complete, and the elapsed time under a 50 s allowance is recorded.
- [ ] 2.3 After release, run one custody-only audit on the served personal vault through the live service and record complete coverage; then archive this change.
