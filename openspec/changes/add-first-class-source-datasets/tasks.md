## 1. Capture behavior

- [x] 1.1 Add failing tests for dataset upload, governed discovery, exact queries and byte identity; record the failing assertions before implementation.
- [x] 1.2 Implement bounded dataset cards in the shared preservation path; verify CSV, TSV, nested JSON, parse limits and no row-value copying with scoped tests.
- [x] 1.3 Implement bounded literal previews for the explicit text export formats; verify encoding, truncation, inert syntax and unchanged original bytes with scoped tests.

## 2. Compatibility and delivery

- [x] 2.1 Verify companion identity, withheld raw reads, atomic rollback and legacy classification with preservation and governance suites; obtain independent review of the implementation.
- [x] 2.2 Document format behavior, record local test/build/lint evidence, run strict OpenSpec validation and the public-artifact gate, and deliver a committed, pushed, ready PR.
- [ ] 2.3 After full CI passes, merge authority and successful integration, synchronize and archive this change through OpenSpec; verify strict validation before and after closure.
