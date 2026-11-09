## 1. Owner refusal on a connector-limited connection

- [x] 1.1 Refuse a collection write by an owner on a connector-limited connection with `COLLECTION_CONNECTOR_LIMITED` and a remediation; every other caller keeps `COLLECTION_NOT_FOUND`.
- [x] 1.2 Pass the collection remediation through `record_memory` errors as `plan_memory` does.
- [x] 1.3 Owner inspect on a limited connection carries a diagnostic that its guards cannot be used for a write.
- [x] 1.4 Integration test: owner revise and validate get the new code with identical words with and without a withheld row; a guest's refusal equals a missing collection's.

## 2. Closure

- [ ] 2.1 Merge, sync the delta into `openspec/specs/records/spec.md` and archive this change.
