## 1. Connection read

- [ ] 1.1 Add one connection read in `working_set` over `EpistemicGraphIndex.neighbors_for` (both directions, caller `keep`, catalogue view), returning per seed the relation key, direction, target ref, title, tier and why; budget-gated, soft-failing to no connections. Check: unit tests for the tier order by behaviours and `generic` only (an invented vault relation with `replaces` ranks first, an invented `associative` one last), inbound `replaces` first, open planning target first within its tier, withheld targets never named, graph unavailable returns none.
- [ ] 1.2 Build the `why`: relation and direction, then the authoring line's sentence (seed page for outbound, other page for inbound), wikilinks stripped, egress-released, at most 140 characters on a word boundary; a bare relation bullet falls back to the target's lede. Check: tests for prose links, relation bullets, inbound context, a sentence that names a withheld page (no why quotes it) and the length bound.
- [ ] 1.3 Replace `_task_links` with the shared read for task leads.

## 2. Allocation and agent default

- [ ] 2.1 In `context_task.compile_context`, admit each lead with its first connection when both fit; leftover connections fill the remaining room in lead order; count omissions. Check: the four-versus-six allowance scenario, a lead served without its connection when only it fits, methods never displaced.
- [ ] 2.2 Render the connection line with its why and the read-when-relevant instruction in `item_line` and the hook renderer; one line in the scaffold skill text. Check: hook tests, scaffold leak test, schema byte budgets unchanged or raised with a recorded reason.
- [ ] 2.3 Record each offered connection's relation, target ref and tier in `served_context`. Check: the record test lists both connections of a two-connection lead.

## 3. Anchors and profile

- [ ] 3.1 Ship `recall` in `packs/core/traversal-profiles.yaml` and set the activation neighbourhood profile to it. Check: recall traverses `derived_from` and `links_to` and excludes `implements`; a vault overlay can extend it.
- [ ] 3.2 Ship the `connections` role (lane `graph`, every anchor kind) and rewrite `_graph_lane` to serve up to four ranked connections per resolved anchor as `pointers[]` with why. Check: the anchor scenario; a withheld page never takes a pointer slot.

## 4. Reconcile overlapping changes

- [ ] 4.1 With the task-context presentation owner's agreement, replace the scenario "Links cost the allowance last" in `recognize-situations-and-project-methods` with a reference to this change's lead-first allocation, and preserve its other scenarios.
- [ ] 4.2 Mark task 8.2 of `promote-referents-without-nudges` as delivered here for the activation profile and typed neighbours as pointers (by the `recall` profile and the `connections` role), leaving its `entity` profile part open. Check: `openspec validate --all --strict` passes.

## 5. Verify and deliver

- [ ] 5.1 Run scoped suites (working set, task context, served context, hooks, traversal profiles, context roles, scaffold leak, egress), ruff, OpenSpec strict and the privacy gate; one independent review with a C4 and C6 verdict.
- [ ] 5.2 Mechanism check on a disposable vault copy: connections served per lead on leads with edges rises from about 0 to at least 1, and warm activation latency stays within the request budget; report before and after.
- [ ] 5.3 Merge, publish the ordinary containing release, install it on the personal service, and confirm from the served-context record that leads carry connections.
- [ ] 5.4 Outcome round on frozen cases with the existing outcome harness: links-last against lead-first, with the graph-off arm as the floor. Report paired differences with 95% intervals, and report a negative result honestly.
- [ ] 5.5 Synchronize the deltas and archive through `openspec archive`; `openspec validate --all --strict` passes before and after.
