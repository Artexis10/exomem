# Tasks

## 1. Contracts

- [ ] 1.1 Define the topic-membership contract in the registries: the membership relation kinds, project-key mapping and page declarations, with direction rules.
- [ ] 1.2 Define the catalogue entry, its projections and the read contract for map, domain-map and profile refs.
- [ ] 1.3 Freeze the evaluation protocol: cases, arms, agent, pull budget, scoring rule for successors and equivalent evidence, tolerance and decision rule.

## 2. Catalogue and projections

- [ ] 2.1 Build the catalogue over the reader's view from index rows, units, memberships and the current-state authority.
- [ ] 2.2 Project the map and the domain maps with bounded output and pagination.
- [ ] 2.3 Compile anchor and topic profiles through the role lanes, cached by generation.
- [ ] 2.4 Resolve map, domain-map and profile refs in `read_memory` with missing and stale states, on every surface.
- [ ] 2.5 Teach the egress guard the new blocks and fields; add counterfactual privacy twins.

## 3. Write side

- [ ] 3.1 Accept an agent-supplied description on the write tools; store marked derived descriptions otherwise, outside Sources and Evidence.
- [ ] 3.2 Add reverse dependencies and generation-bound invalidation to derived sync for the catalogue, projections and profiles.
- [ ] 3.3 Backfill descriptions for existing entries: resumable, with provenance, cost and coverage reported.

## 4. Activation and clients

- [ ] 4.1 Serve the `maps` packet mode behind the setting, with `anchors` as the default and one packet budget.
- [ ] 4.2 Update the server instructions, the `activate_context` description, the skill scaffold and the retrieve hook for map-assisted pulls before the first answer.

## 5. Evaluation and switch

- [ ] 5.1 Iterate on the development cases: map with pulls, and map plus turn slice with pulls, against `anchors` with the same pulls.
- [ ] 5.2 Run the frozen held-out evaluation once, blind.
- [ ] 5.3 If it passes, switch the default through the release line, then replay field reports on the installed release.
