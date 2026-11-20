# Tasks

## 1. Discovery

- [x] 1.1 Make the published-identity reader return no identity only when the store holds none, and raise a distinct error when it cannot read the store. Verify: a test with no published space receives `unavailable`.
- [x] 1.2 Report `unknown` with a null id when discovery cannot read the store. Verify: a test that holds the store lock past the wait receives `unknown`.
- [x] 1.3 Wait a bounded time for the store lock. Verify: a test that holds the lock briefly receives `observed`; four concurrent discovery calls on a cell report the published space.

## 2. Closure

- [x] 2.1 Synchronize the delta into the canonical spec and archive this change with `openspec archive`. Verify: `openspec validate --all --strict` passes before and after the archive.
