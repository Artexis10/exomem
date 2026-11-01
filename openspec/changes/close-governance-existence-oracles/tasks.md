## 1. Byte parity (restores the withheld-equals-missing contract)

- [x] 1.1 `op_suggest_links` refuses a withheld seed page through `_refuse_withheld_page_seed`, as an absent page refuses.
- [x] 1.2 `op_list_inbound_links` omits `target` for a restricted caller on every outcome. On a governed vault, the dispatcher holds a restricted caller's empty outcome (a withheld or a missing target) to F; a visible target is not held (design D2). Measured in process for a restricted caller, 40 warm calls per target, completion median / minimum / maximum in ms: withheld 55.22 / 55.19 / 55.29, a 324 KB withheld page 55.21 / 55.19 / 55.27, missing 55.22 / 55.19 / 55.30; work before the hold, median / maximum: 12.95 / 17.15, 12.98 / 16.91, 11.82 / 16.72. A visible target is not held: 12.79 / 10.94 / 16.60. Through MCP, 20 calls per target, the surface held both empty outcomes (medians 75.4 and 73.6 ms) with no thread pad; a visible target took 38.0 ms.
- [x] 1.3 The error postfilter exempts the caller's own references from the arguments that the command registry declares as path roles (`reserved_paths.path_roles_for_command`). Compile, review-compilation, inbound-links and recovery errors then echo the caller's own path for a withheld and a missing reference. A temporary set in `egress.py` keeps the exemption for reference arguments that the registry does not declare yet.
- [x] 1.4 `query_data.query_data` authorizes the relative and the resolved spelling before any existence, type, suffix or exclusion check, and every refusal uses the missing-path text.

## 2. Contract changes

- [x] 2.1 Mark `browse_memory`, `compile_source`, `connect_memory` and `query_dataset` with `restricted_refusal_floor`, measure each door's warm withheld refusal work, and record the numbers here; raise F only if a door exceeds it.
  Measured in process for a restricted caller, 40 warm calls per door, work before padding, median / maximum in ms against F = 55 ms: `browse_memory` folder 7.1 / 10.1, `connect_memory` suggest-links 9.3 / 12.5, `compile_source` 14.0 / 17.7, `query_dataset` 8.0 / 10.7. Every door is under F, so F stays 55 ms. The `review_memory` compilation route is a separate command name and carries no floor attribute; its withheld and absent medians measured 21.2 and 22.2 ms.
- [x] 2.2 `emit_boundary_receipt` appends an empty-outcome disclosure receipt for a restricted caller only, without an extra policy load.
- [x] 2.3 `verify_chain` reports `anchored` per instance and `unanchored_instances`; the audit category reports unanchored instances as warnings.
- [x] 2.4 `govern_memory list` and `coordination_status` return rule ids, grant ids and contention to the owner only.

## 3. Verification

- [x] 3.1 Scoped tests: one withheld-against-missing pair per door, the receipt-store fault for an absent path and a zero-hit search, an unanchored copy, and the non-owner metadata views.
- [ ] 3.2 The governance audit rig's twin-world, receipt and timing phases show no oracle on these doors.
- [ ] 3.3 Full suite.
- [ ] 3.4 Author-independent review of the diff, and a recheck of each correction.

## 4. Closure

- [ ] 4.1 After merge, sync these deltas into the canonical specs and archive the change with `openspec archive`.
