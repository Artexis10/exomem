# Tasks

## 1. Candidate admission and warnings

- [x] 1.1 Add a capture regression with one legacy original beside valid receipts; prove create, reuse, warnings and unchanged legacy bytes.
- [x] 1.2 Exclude legacy candidates and return warnings through capture results; preserve the existing malformed-receipt and authority tests.

## 2. Delivery

- [x] 2.1 Obtain independent review, pass privacy and strict specification checks, merge and publish the ordinary runtime patch.

PR #2127 merged as `be0f9cc9e73dbfe27033717560299fa989ba5fcd` after independent review and 45 successful checks, with 13 configured skips.
Review: https://github.com/Artexis10/exomem/pull/2127#issuecomment-6418201235.
Release `v0.147.0` contains that commit; `v0.148.0` retains it.
The closure check passed all 243 strict specification checks before archiving.
