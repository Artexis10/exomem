# Tasks

## 1. Contracts

- [x] 1.1 Define the topic-membership contract: the page's `domain` field, the project-key `domain` field, the `membership` relation family, resolution through the domains registry, direction and non-transitivity (design D3).
- [x] 1.2 Define the context-catalogue entry, the description order and the `summary` field, the derived-description store, lifecycle class and activity, map selection, refs, and the read contract with page size and continuation (design D11).
- [x] 1.3 Freeze the evaluation protocol: cases, arms (including pull instructions with no map), agent, pull budget, scoring rule for successors and equivalent evidence, contamination tolerance and decision rule (design, Evaluation).

## 2. Catalogue and projections

- [ ] 2.1 Build the context-catalogue sidecar over the caller's view from index rows, units, memberships and the current-state authority. Parse the project-key `domain` field into the project-key registry and its content hash, and add the catalogue component to derived sync.
- [ ] 2.2 Project the map and the domain maps with bounded output and pagination.
- [ ] 2.3 Compile anchor and topic profiles through the role lanes, cached by generation.
- [ ] 2.4 Resolve map, domain-map and profile refs in `read_memory` with missing and stale states, on every surface.
- [ ] 2.5 Teach the egress guard the new blocks and fields; add counterfactual privacy twins.

## 3. Write side

- [ ] 3.1 Accept an agent-supplied description on the write tools and store it in the page's `summary` field. Resolve `domain` through the source-capture resolver on every write that sets it.
- [ ] 3.2 Add reverse dependencies and generation-bound invalidation to derived sync for the catalogue, projections and profiles.
- [ ] 3.3 Report description coverage by origin on the first catalogue build. If a development round shows that lede descriptions lose against authored ones, register the derived-description store as a reserved internal-state path with its own owner, and run a resumable model backfill into it with provenance, cost and coverage reported.

## 4. Activation and clients

- [ ] 4.1 Serve the `maps` packet mode behind the setting, with `anchors` as the default and one packet budget; register its stages under instrumentation and extend the latency gate to `maps` mode.
- [ ] 4.2 Update the server instructions (within their 900-character bound, keeping the user's words verbatim as `turn`), the `activate_context` description, the skill scaffold, and the retrieve hook's rendering of a map, for map-assisted pulls before the first answer. Regenerate the surface pins: schema fidelity fixture, digests, `docs/capabilities.md`, README table and plugin trees.

## 5. Evaluation and switch

- [ ] 5.1 Iterate on the development cases: map with pulls, map plus turn slice with pulls, and pull instructions with no map, against `anchors` with the same pulls.
- [ ] 5.2 Run the frozen held-out evaluation once, blind.
- [ ] 5.3 If it passes, modify the "Packet modes" requirement's default, switch the default through the release line, then replay field reports on the installed release.
