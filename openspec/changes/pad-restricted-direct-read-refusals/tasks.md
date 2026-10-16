## 1. Floor

- [x] 1.1 Add `RESTRICTED_REFUSAL_FLOOR_MS` and the pad-only `pad_to_refusal_floor` to `src/exomem/governance/projection_timing.py`, with no environment override and nothing added to `PUBLIC_REQUEST_CLASSES`.
- [x] 1.2 Add the `restricted_refusal_floor` attribute to `Command` and set it in the registry metadata of `read_memory`, `get` and `fetch`.
- [x] 1.3 Wrap `writer_lease.invoke_command` beside `_fixed_projected_command_completion` and outside `_collection_store_thread`: start the clock at entry, ask `caller_restricted` only after a refusal, free the refusal's finished frames, wait for the floor, record `command.refusal_floor` with its `overrun` flag, and raise the same exception.

## 2. Early decision

- [x] 2.1 Give the body of `annotate_page` a decide-only exit after its withheld decision and receipt, exposed as `egress.page_withheld_entirely`. In `op_get`, for a restricted caller, call it right after the page read and refuse with the missing-path text when the page is withheld entirely, before history, links, the query log and the heat signal. Keep the owner order. A direct comparison of one restricted read sequence (withheld, notice and released pages, each with and without links and history) before and after the change gave identical responses and identical disclosure receipts; only the query-log rows of the refused withheld reads are gone.

## 3. Verification

- [x] 3.1 Red first: `tests/test_restricted_refusal_floor.py` reads a withheld path, a hidden-only reference and a unit reference on the withheld page as `external`, then the same three targets after the page is deleted, and asserts that each refusal takes at least the floor and that each pair is byte-identical. It fails with the dispatcher change removed. Its owner case fails when the restriction check is removed, its notice case fails when successes are padded or when the early decision refuses notices, and its receipt case fails when the early decision uses `release_level_for`.
- [x] 3.2 Document `command.refusal_floor` and the second `read.release` span in `docs/observability.md`, and pin the span name in `tests/test_call_spans.py`.
- [x] 3.3 Run the scoped suites one module at a time: the direct-read payload, egress, memory-reference, corpus-context cache, plan-motivation, call-span, unit-read, schema, plugin and privacy modules, and every module that calls `op_get`, `read_memory` or `caller_restricted`.
- [ ] 3.4 Run an author-independent review of the diff and recheck each correction.

## 4. Closure

- [ ] 4.1 After merge, synchronize these deltas into `openspec/specs/get-payload-shape/spec.md` and `openspec/specs/stable-memory-references/spec.md`, then archive the change with `openspec archive`.
