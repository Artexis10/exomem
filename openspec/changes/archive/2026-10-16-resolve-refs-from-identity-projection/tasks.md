## 1. Resolution

- [x] 1.1 Keep the per-identity reference owners on the identity census and expose them on the reference identity snapshot; check that a duplicated identity keeps every owner.
- [x] 1.2 Resolve from the current identity projection in `memory_refs.paths_for_ids_read_only`, confirm it is still current after the lookup, and fall back to the scan otherwise; check that the read builds, fills or refreshes nothing.
- [x] 1.3 Apply one membership rule to both sources: any-case `.md` suffix in the scan, newline-normalized frontmatter in the census, and the scan for every vault whose Knowledge Base root holds `trash/`; check that both sources answer alike for a duplicate, a `.MD` page and a CR-only page, and that a `trash/` page resolves as today.
- [x] 1.4 Add the `snapshot` and `cpu_ms` fields to the `memory_refs.identity_census` span and update `docs/observability.md`.

## 2. Verification

- [x] 2.1 Test through a current projection: a visible and hidden twin resolves to the visible page and two visible twins read as ambiguous, both as through the scan; an absent reference and a hidden-only reference give the same response; a pending external edit makes resolution scan and answer; a `.MD` page and a CR-only page resolve alike through both sources; a vault with `trash/` scans once with the projection current and resolves its `trash/` page.
- [x] 2.2 Extend the scan-count disclosure test: present and absent references cause zero scans with the projection current and one scan with it not current.
- [x] 2.3 Run an author-independent review of the diff and recheck each correction.

## 3. Closure

- [x] 3.1 After merge, synchronize these deltas into `openspec/specs/stable-memory-references/spec.md` and `openspec/specs/planning/spec.md`, then archive the change with `openspec archive`.
