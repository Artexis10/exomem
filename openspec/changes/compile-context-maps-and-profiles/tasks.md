# Tasks

## 1. Contracts

- [x] 1.1 Define the topic-membership contract: the page's `topics` field, the project-key `domain` field, the `membership` relation family, resolution through the domains registry, direction and non-transitivity (design D3).
- [x] 1.2 Define the context-catalogue entry, the description order and the `summary` field, the derived-description store, lifecycle class and activity, map selection, refs, and the read contract with page size and continuation (design D11).
- [x] 1.3 Define the evaluation protocol: cases, arms (including pull instructions with no map), agent, pull budget, scoring rule for successors and equivalent evidence, contamination tolerance and decision rule (design, Evaluation).

## 2. Catalogue and projections

- [x] 2.1 Build the context-catalogue sidecar over the caller's view from index rows, units, memberships and the current-state authority. Parse the project-key `domain` field into the project-key registry and its content hash, and add the catalogue component to derived sync. Read the raw-material, schema, governance and archive trees from the product's layout declaration that activation reads: activation walks archive trees and ranks them down, and the catalogue excludes them from notes. Add the `domain` field to the project-key content hash only when a key sets it, so an upgrade alone does not rebuild the activation index.
- [x] 2.2 Project the map and the domain maps with bounded output and pagination.
- [x] 2.3 Compile anchor and topic profiles through the role lanes, per request over the caller's candidate entries (no build cache: a cache key could not cover every input the build reads).
- [x] 2.4 Resolve map, domain-map and profile refs in `read_memory` with missing and stale states, on every surface.
- [x] 2.5 Teach the egress guard the new blocks and fields; add counterfactual privacy twins.

## 3. Write side

- [x] 3.1 Add `summary` and `topics` parameters to the write tools that create or edit pages, stored in the page's `summary` and `topics` fields; read edits made outside Exomem from the raw frontmatter. Resolve `topics` through the source-capture domain resolver and policy on every write that sets it. Document `summary` and `topics` in the scaffold's frontmatter reference, and the project-key `domain` field in the format comment of `project-keys.yaml`.
- [x] 3.2 Add reverse dependencies and generation-bound invalidation to derived sync for the catalogue, projections and profiles.
- [ ] 3.3 Report description coverage by origin on the first catalogue build. If a development round shows that lede descriptions lose against authored ones, register the derived-description store as a reserved internal-state path with its own owner, and have a maintenance agent run a resumable model backfill into it, with provenance, cost and coverage reported. The server makes no model call.

## 4. Activation and clients

- [x] 4.1 Serve the `maps` packet mode behind the setting, with one packet budget; register its stages under instrumentation and extend the latency gate to `maps` mode.
- [x] 4.2 Update the server instructions (within their 900-character bound, keeping the user's words verbatim as `turn`), the `activate_context` description, the skill scaffold, and the retrieve hook's rendering of a map, for map-assisted pulls before the first answer. Regenerate the surface pins, including the write-tool parameters from 3.1: schema fidelity fixture, digests, `docs/capabilities.md`, README table and plugin trees.

## 5. Evaluation and switch

- [ ] 5.1 Build the evaluation inputs: extend the agent-in-the-loop harness with arms A, M and P and pass a dry run; sample the ordinary turns; have an independent key writer key the cases against the snapshot; fix the groups.
- [ ] 5.2 Iterate on the development cases and development ordinary turns with arms A, M and P. Probe the turn slice here only. End with the power step on the build that will be frozen.
- [ ] 5.3 Optional, later, and not a gate: make the freeze commit for one unbiased measure. Record the protocol, arm hashes, hashed held-out ids, groups, and the power step's spread, sizes, δ values and simulated pass rates.
- [ ] 5.4 Optional, later, and not a gate: run the frozen held-out evaluation once, blind, on the sealed split. Report each comparison. No result changes the default by itself.
- [ ] 5.5 Switch the default of `EXOMEM_ACTIVATION_PACKET` to `maps` by operator ruling (design amendment 12) through the release line. Document `EXOMEM_ACTIVATION_PACKET=anchors` as the rollback. Replay field reports on the installed release.

- [ ] 5.6 Build the turn slice, shipped on by default with `EXOMEM_ACTIVATION_TURN_SLICE=off` as the rollback, over the one guarded anchors compile (design D13, amendment 13). Prove it on the activation audit fixtures, with the caller-view twins, the failure-keeps-the-map case, the packet-restored case with the setting off, and the latency gate.

## 6. Preparation recovery

- [ ] 6.1 Scope shared working-set collection as an internal producer. Prove that maintenance keeps audit files unchanged while external reads retain disclosure and authorization.
- [ ] 6.2 Add explicit successor preparation from an unproven checkpoint. Preserve the failed archive and opaque inputs, and refuse ordinary retry before destructive fallback.
- [ ] 6.3 Converge media before publication and verify exact final vector coverage through existing product checks. Prove same-mtime edits and new media parents cannot inherit stale coverage.
- [ ] 6.4 Qualify the integrated source through independent review and current CI, then verify bounded recovery and fresh quiescence before any development generation.
- [ ] 6.5 File the evaluation reads' receipts out of the vault into run evidence, and prove that a governed vault returns with identical bytes and signatures after the startup proof and after a session (design, amendment 9).
- [ ] 6.6 Preserve usable registry entries through provenance removal, verify effective entries and narrative ancestry, and rebuild state against the corrected snapshot digest. Keep authored workflow checks separate from switch statistics (design, amendment 11).
