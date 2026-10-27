# Tasks

## 1. Compact page-hit excerpt

- [x] 1.1 Emit the page-hit excerpt in the compact serializer and cut it after release stripping in the egress projector, at one named length.
- [x] 1.2 Add tests: a bounded compact excerpt, an excerpt that a ceiling withholds, and a compact excerpt that is a prefix of the stripped full excerpt.
- [x] 1.3 Update the compact-shape tests and the `find` documentation that stated compact hits omit the excerpt.
- [x] 1.4 Record the compact payload size before and after on the fixture vault.
- [x] 1.5 Trim a fragment of a stripped identity from an excerpt that a length cut left inside its link, in the one strip step, and add a regression test for compact and full mode.

## 2. Delivery

- [ ] 2.1 Review the exact change independently, with the release plane as the main risk; resolve findings.
- [ ] 2.2 Merge, then synchronize this delta into the canonical specs and archive this change with `openspec archive`.
