## 1. Connection read

- [x] 1.1 Add one connection read in `working_set` over `EpistemicGraphIndex.neighbors_for` (both directions, caller `keep`, catalogue view), returning per seed the relation key, direction, target ref, title, tier and why; budget-gated, soft-failing to no connections. Check: unit tests for the tier order by behaviours and `generic` only (an invented vault relation with `replaces` ranks first, an invented `associative` one last), inbound `replaces` first, open planning target first within its tier, withheld targets never named, graph unavailable returns none.
- [x] 1.2 Build the `why`: relation and direction, then the authoring line's sentence (seed page for outbound, other page for inbound), wikilinks stripped, egress-released, at most 140 characters on a word boundary; a bare relation bullet falls back to the target's lede. Check: tests for prose links, relation bullets, inbound context, a sentence that names a withheld page (no why quotes it) and the length bound.
- [x] 1.3 Replace `_task_links` with the shared read for task leads.

## 2. Allocation and agent default

- [x] 2.1 In `context_task.compile_context`, admit each lead with its first connection when both fit; leftover connections fill the remaining room in lead order; count omissions. Check: the four-versus-six allowance scenario, a lead served without its connection when only it fits, methods never displaced.
- [x] 2.2 Render the connection line with its why and the read-when-relevant instruction in `item_line` and the hook renderer; one line in the scaffold skill text. Check: hook tests, scaffold leak test, schema byte budgets unchanged or raised with a recorded reason.
- [x] 2.3 Record each offered connection's relation, target ref and tier in `served_context`. Check: the record test lists both connections of a two-connection lead.

- [x] 2.4 Set `context_views.TURN_SLICE_SHARE` to 0.5 and the default packet size to 6,000 characters in `working_set.DEFAULT_BUDGET_CHARS`, the hook's `_WORKING_SET_MAX_CHARS`, the public tool schema and generated contracts, and the docs that state 4,000. Check: the default request reports `limit_chars` 6,000; the map keeps its items at 6,000; maintained producers regenerate contract stamps; schema byte budgets pass.

## 3. Anchors and profile

- [x] 3.1 Ship `recall` in `packs/core/traversal-profiles.yaml` and set the activation neighbourhood profile to it. Check: recall traverses `derived_from` and `links_to` and excludes `implements`; a vault overlay can extend it.
- [x] 3.2 Ship the `connections` role (lane `graph`, every anchor kind) and rewrite `_graph_lane` to serve up to four ranked connections per resolved anchor as `pointers[]` with why. Check: the anchor scenario; a withheld page never takes a pointer slot; the `graph` lane runs beside the six lenses and never displaces one.

## 4. Reconcile overlapping changes

- [x] 4.1 Under the operator-approved plan, with the task-context presentation owner asked to review, replace the scenario "Links cost the allowance last" in `recognize-situations-and-project-methods` with a reference to this change's lead-first allocation, and preserve its other scenarios.
- [x] 4.2 Mark task 8.2 of `promote-referents-without-nudges` as delivered here for the activation profile and typed neighbours as pointers (by the `recall` profile and the `connections` role), leaving its `entity` profile part open. Check: `openspec validate --all --strict` passes.
- [x] 4.3 Keep the stated activation default consistent in the active changes that restate it (`recognize-situations-and-project-methods`, `close-memory-loop`, `replace-agent-tool-surface`).

## 5. Verify and deliver

- [x] 5.1 Run scoped suites (working set, task context, served context, hooks, traversal profiles, context roles, scaffold leak, egress), ruff, OpenSpec strict and the privacy gate; one independent review with a C4 and C6 verdict. Done: 81 scoped files on Python 3.11, 3,443 passed with one failure that also fails on main; independent Opus review approved #2924 after one correction round (6 blocking findings fixed), C4 and C6 absent; follow-ups approved in #2927.
- [x] 5.2 Mechanism check on a disposable vault copy: connections served per lead on leads with edges rises from about 0 to at least 1, and warm activation latency stays within the request budget; report before and after. Done, target partly met: on a vault copy, 60 frozen real query turns, links per lead rose from 0.086 to 0.70 (about 0.87 per lead with edges, inferred from the 80.7% edge share; the packet budget still cuts some). Paired latency: +1.22 s per activation (95% interval 0.56 to 1.88 s) on an 18 s in-process median under host load; reason building was then made lazy in #2927. The request budget is unverified for the shipped build: no latency was measured after #2927.
- [x] 5.3 Merge, publish the ordinary containing release, install it on the personal service, and confirm from the served-context record that leads carry connections. Done: shipped in 0.191.0 (#2924 and #2927). From the served-context records on 2026-11-20, 23 of 28 leads carried connections after the graph republished, against 20 of 9,797 leads in the 7 days before. A 22-minute window after promotion served none because graph reads were withdrawn; tracked in #2947.
- [x] 5.4 Outcome round on frozen cases with the existing outcome harness: links-last against lead-first, with the graph-off arm as the floor. Report paired differences with 95% intervals, and report a negative result honestly. Done: 12 frozen held-out cases × 2 repeats (OpenAI actor, Claude Opus judge, re-derived by an independent evaluator): lead-first 24/24, main 15/24, graph off 13/24; lead-first minus main +0.375 (95% interval 0.125 to 0.667); controls unchanged. Kept: lead-first beat main, and the interval excludes zero.
- [x] 5.5 Synchronize the deltas and archive through `openspec archive`; `openspec validate --all --strict` passes before and after. Done: archived with `openspec archive`; strict validation passed before and after.
