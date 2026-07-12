## 1. Design + spec

- [ ] 1.1 Enumerate every `connect_memory` and `adopt_vault` operation and classify read vs write from the existing dispatch; confirm the classification source of truth.
- [ ] 1.2 Choose approach A (per-operation predicate, no schema change) vs B (split surface); finalize `specs/lease-operation-scope/spec.md`. `openspec validate scope-lease-to-write-operations --strict`.

## 2. Implement

- [ ] 2.1 Thread the resolved operation's read-only-ness into `writer_lease.invoke`'s bypass decision (fail SAFE: unknown → require lease).
- [ ] 2.2 Keep write operations lease-gated; do not change the read/write classification of any operation.
- [ ] 2.3 If (and only if) the tool surface must change, regenerate + review `tests/fixtures/mcp_tool_schemas.json`.

## 3. Verify

- [ ] 3.1 New test: with an unreachable `EXOMEM_WRITER_LEASE_URL`, a default `connect_memory` (suggest-links) and `adopt_vault` (scan) succeed; `create-entity` / `accept-relation` / an adopt write mode still refuse `WRITER_COORDINATOR_UNAVAILABLE`.
- [ ] 3.2 `tests/test_mcp_schema_fidelity.py` green (surface unchanged, or baseline deliberately updated).
- [ ] 3.3 `tests/test_writer_lease.py` and connect/adopt tests stay green.
- [ ] 3.4 `uvx ruff check` scoped to changed files only.
