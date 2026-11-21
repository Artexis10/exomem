# Tasks

## 1. Reuse verified Source chunks under the published corpus proof

- [x] 1.1 Keep the selected chunks of each verified Source, keyed by the published page hash and fenced by the eligible-set proof. Verify: a repeated passage request reads no Source page.
- [x] 1.2 Read a Source again when its published hash or the proof changes. Verify: an edited and republished Source is read again and its stale chunk is withheld, and an erased Source is withheld while the index still selects it.
- [x] 1.3 Bound the memory by entry count and bytes. Verify: each bound holds after a request that verifies more Sources than it allows.
- [x] 1.4 Measure one and four callers, idle and during a capture, in the production envelope before and after the change, with capture duration.

## 2. Closure

- [x] 2.1 Synchronize the delta into the canonical spec and archive this change with `openspec archive`. Verify: `openspec validate --all --strict` passes before and after the archive.
