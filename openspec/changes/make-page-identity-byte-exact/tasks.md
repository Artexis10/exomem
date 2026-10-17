# Tasks

## 1. Canonical identity

- [ ] 1.1 Classify text-read-to-hash call sites; repair canonical identity and guard readers, with CRLF and concurrent-change regressions.
- [ ] 1.2 Preserve existing newline bytes during ID backfill; verify exact installed bytes and existing identity refusals.
- [ ] 1.3 Verify derived recovery over a legacy hash without changing canonical bytes.

## 2. Delivery

- [ ] 2.1 Run affected suites, the complete CI corpus, privacy checks, and strict OpenSpec validation; obtain independent review of the integrated batch.
- [ ] 2.2 Merge and release the reviewed bytes, verify installed identity and custody, then synchronize and archive this change through OpenSpec.
