# Tasks

## 1. Canonical identity

- [x] 1.1 Classify text-read-to-hash call sites; repair canonical identity and guard readers, with CRLF and concurrent-change regressions.
- [x] 1.2 Preserve existing newline bytes during ID backfill; verify exact installed bytes and existing identity refusals.
- [x] 1.3 Verify derived recovery over a legacy hash without changing canonical bytes.
- [x] 1.4 Share BOM-aware frontmatter parsing and invalidate old reference and semantic interpretations through their existing versions.

## 2. Delivery

- [x] 2.1 Run affected suites, the complete CI corpus, privacy checks, and strict OpenSpec validation; obtain independent review of the integrated batch.
- [x] 2.2 Merge and release the reviewed bytes, verify installed identity and custody, then synchronize and archive this change through OpenSpec.

## Verification

PR #1838 delivered the guarded readers, byte-preserving backfill, shared frontmatter parser, and derived parser-version updates.
Its [independent review](https://github.com/Artexis10/exomem/pull/1838#issuecomment-6243820869) approved the integrated implementation after corrections.
PR #1875 delivered the remaining traversal-profile guards.
The [full release-source run](https://github.com/Artexis10/exomem/actions/runs/39155005719) passed before release 0.127.5.

Installed release 0.127.5 passes the existing byte-identity, backfill, legacy-recovery, and traversal-profile checks in disposable state.
The verification hides live state and blocks network access.
These checks establish byte identity and guarded custody; they make no write-latency claim.
