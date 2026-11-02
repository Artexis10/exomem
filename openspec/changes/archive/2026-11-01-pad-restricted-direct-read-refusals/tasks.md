## 1. Floor

- [x] 1.1 Add `RESTRICTED_REFUSAL_FLOOR_MS` and the pad-only `pad_to_refusal_floor`, with its spinning tail, to `src/exomem/governance/projection_timing.py`, with no environment override and nothing added to `PUBLIC_REQUEST_CLASSES`.
- [x] 1.2 Add the `restricted_refusal_floor` attribute to `Command` and set it in the registry metadata of `read_memory`, `get` and `fetch`.
- [x] 1.3 Wrap `writer_lease.invoke_command` beside `_fixed_projected_command_completion` and outside `_collection_store_thread`: start the clock at entry, ask `caller_restricted` only after a refusal, free the refusal's finished frames, hand the wait to the surface or wait on the thread, record `command.refusal_floor` with `work_ms`, `overrun` and `deferred`, and raise the same exception.
- [x] 1.4 Wait out a pending floor in an outermost MCP middleware and around the REST handler, after their own handling of the refusal: an `await` on the loop, and the last 3 ms on a worker thread.
- [x] 1.5 Obsolete, not shipped: a logging filter for coded refusals. The command surface answers coded refusals with the error envelope, so FastMCP no longer logs them (design D6).

## 2. Early decision and refusal work

- [x] 2.1 Give the body of `annotate_page` a decide-only exit after its withheld decision and receipt, exposed as `egress.page_withheld_entirely`. In `op_get`, for a restricted caller, call it right after the page read and refuse with the missing-path text when the page is withheld entirely, before history, links, the query log and the heat check. Keep the owner order.
- [x] 2.2 Answer every policy load inside one direct read from its first load.
- [x] 2.3 Keep the reference gate's census per vault, list again only changed directories, rebuild the alias tables only when a file name changed, and replace the pattern of all aliases with set lookups that give the same matches.
- [x] 2.4 Set F from the refusal work measured at 300, 1,500 and 3,000 files.

## 3. Verification

- [x] 3.1 `tests/test_restricted_refusal_floor.py` reads a withheld path, a hidden-only reference and a unit reference on the withheld page as `external`, then the same three targets after the page is deleted, and asserts that each refusal takes at least the floor and that each pair is byte-identical. Its owner case fails when the restriction check is removed, its notice case fails when successes are padded or when the early decision refuses notices, and its receipt case fails when the early decision uses `release_level_for`.
- [x] 3.2 The same module drives a withheld and a missing refusal through the in-memory MCP stack and compares their median completion times; that case fails on the change before the correction round. Its read-log case fails when the early decision is removed, and its log-line case fails when a coded refusal keeps its traceback.
- [x] 3.3 `tests/test_governance_postfilter.py` names a page created after an earlier error; it fails when the census is reused without a walk.
- [x] 3.4 Document `command.refusal_floor` and the second `read.release` span in `docs/observability.md`, and pin the span name in `tests/test_call_spans.py`.
- [x] 3.5 Run the scoped suites one module at a time: the direct-read payload, egress, postfilter, receipts, memory-reference, server, REST, call-span, unit-read, schema, plugin and privacy modules, and every module that calls `op_get`, `read_memory` or `caller_restricted`.
- [x] 3.6 Run an author-independent review of the diff and recheck each correction. Evidence: independent Opus lane-reviewer APPROVE on PR #1797.

## 4. Closure

- [x] 4.1 After merge, synchronize these deltas into `openspec/specs/get-payload-shape/spec.md` and `openspec/specs/stable-memory-references/spec.md`, then archive the change with `openspec archive`.
