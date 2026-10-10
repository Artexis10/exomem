# Tasks

Each task names its check.
Run the scoped suites a task touches during development, and the full corpus at the completion boundary (task 9.1).
Design decisions are cited as D1 to D8.

## 1. Authority contract (D2)

- [ ] 1.1 Replace the served v1 authority rule (`commands.py:1313-1317`) and the scaffold `proactive_capture` row with the D2 rule. Check: a bootstrap contract test first fails on the text "v1 writers retain confirmation", then passes; a v2-activated fixture still serves the v2 rule unchanged; the scaffold no-leak and skill-contract tests pass.
- [ ] 1.2 Pin the D2 classes at their leaves. Check: an owner's type save, relation-extension save and typed edge commit with no confirmation argument; a resolved non-owner's relation save stays a pending item; a v2-activated vault without a grant refuses an entity creation while the primary write commits; an upgrade of a v1 fixture leaves it v1.

## 2. Registry data and display (D3, D6)

- [ ] 2.1 Add `label`, `generic` and `endpoints` to the relation adapter and the core pack, with validation findings for an unknown family, an extension side outside its parent's, and other than one active generic relation. Check: registry tests cover save, history and restore round trips with the new fields, endpoint inheritance, a narrower and a wider extension, and parity of every existing key.
- [ ] 2.2 Add `relation_registry.display_label` and emit `relation_label` from every producer named in D6. Check: one test per producer through its public door asserts the label of a core relation, of an extension (`<label> (a kind of <parent label>)`) and of a shared label with its namespace; schema-fidelity tests show every existing key field unchanged.
- [ ] 2.3 Replace the `relates_to` key comparisons on the touched paths (`mutation_terminal.py:1402`, `vocabulary_signals.py:71`, `relation_census.py:60`, `link.py:391`) with the `generic` attribute. Check: the semgrep C6 gate passes; the existing advisory, signal and census tests pass unchanged; the PR lists the remaining literals as C4 debt.

- [ ] 2.4 Add a symmetric kinship relation and an affiliation relation to the core pack with labels, families and endpoints (R2). Check: the registry parity test lists both; a vault extension can parent on each and joins the `entity` traversal profile through its family.
- [ ] 2.5 Promote a parentless entity type with a `new_family` notice (R1). Check: an owner's declaration of an unregistered root type creates it with no confirmation, the next session's first notice names it as a new family, and one call reverts the type and its entities.

## 3. Typed edges (D3)

- [ ] 3.1 Accept `{target, relation}` items in `create-entity` `connections`. Check: a typed item renders its predicate; a string renders the generic relation; an unknown predicate refuses with its finding and writes nothing; MCP, CLI and REST accept the same shape.
- [ ] 3.2 Add the `add-relation` operation and curation step kind. Check: one bullet and one log entry per call; a repeat returns `exists` with unchanged bytes; a stale hash refuses; an unregistered predicate refuses with the propose route; withheld and missing targets refuse byte-identically; an episode `prepare` and `resume` runs the step and `propose-compensation` removes it; the operation is classified in the egress selector table and the credential and owner-control matrix.
- [ ] 3.3 Add `specific_options` to `relation_advisory`. Check: a person-to-animal generic edge written through `add-relation`, through a `## Relations` bullet in `remember` and through a string connection each offers `owns`; a note-to-entity generic edge offers nothing; a predicate without endpoints is never offered; at most 4 options; a dismissal suppresses the same offer by fingerprint; no write refuses.

## 4. Referent declarations (D1)

- [ ] 4.1 Write pure-logic tests for declaration validation and outcome mapping before wiring. Check: the tests fail first, then pass, for bounds, the evidence substring after normalization, relation endpoint naming, and each of the six outcomes.
- [ ] 4.2 Add `referents` to `remember`, `observe_memory`, `edit_memory` and `capture_source` through the canonical command definitions and generated surfaces. Check: schema-fidelity tests across MCP, CLI and REST; a malformed argument refuses before any write.
- [ ] 4.3 Run declarations after the primary commit through `resolve-entity`, `create-entity` and `add-relation`, with receipt binding. Check: reuse, creation, `needs_decision` followed by a fingerprint-bound `create-entity`, `needs_type` and `type_unknown`; an identical retry creates nothing twice; an injected stop after the primary commit is completed once by the identical retry.
- [ ] 4.4 Write the provenance log entries. Check: a created entity's log and an edge subject's log name the originating path, the operation id and the evidence span; an episode leaf adds its episode key.
- [ ] 4.5 Teach the channel: the core capture line with its `CORE_RULES` entry, the `vocabulary` section, `references/vocabulary.md` and the four tool descriptions; then regenerate the skill stamps, plugins, tool schemas, hosted candidate, cloud plugin and capability docs. Check: the compact budget tests pass and the PR records the new core bytes at the default and `maximal` levels; no-leak, plugin-sync, hosted-descriptor and egress-receipt suites pass; the tool-surface pin moves once with a reason and the ChatGPT pending digest is updated.

## 5. Surfacing and revert (D5)

- [ ] 5.1 Add the `recent_promotions` due-state category. Check: a promotion from session A is absent in A, counted once in B and absent in C; a revert and a dismissal settle it; a quiet family is not counted; a withheld promotion adds nothing to a restricted audience's count in a twin fixture.
- [ ] 5.2 Add the revert routes to the item context. Check: each route runs as one call for a registry addition (with `also_removes` when a later save exists), an entity creation (recoverable from `_trash`) and a typed edge; a changed page refuses the edge revert; history and logs keep both records.

## 6. Activation over typed families (D4)

- [ ] 6.1 Move the built-in traversal profiles into `packs/core/traversal-profiles.yaml` with a parity check, then delete the code dict and the parity check. Check: graph-context output is identical for every former built-in on the existing graph fixtures.
- [ ] 6.2 Add the `entity` profile and `activation_anchor_kinds`, and select the profile per anchor kind in the graph lane. Check: the invented-name test reaches a supplement two typed hops from a person only through `entity`; a hub anchor's packet is unchanged; an extension under `owns` is traversed with no code change; the negative controls and the continuity group pass; three matched latency pairs stay within +10% of the base.

## 7. Acceptance (D8)

- [ ] 7.1 Build the end-to-end replay on a synthetic product-shaped corpus: an invented person entity and earlier notes, a scripted decider that saves one entity type and writes the rich turn about the person's dog, its supplement and its diet with `referents`, the real publication pipeline, and a fresh-session `activate_context`. Check: the dog is an entity of the saved type; the person-to-dog ownership edge is in the graph; the fresh-session packet resolves the dog and serves the owner context; the incidental-name and one-off-mention twins produce no entity, edge or notice; a per-case table is recorded.
- [ ] 7.2 Add the disclosure twin across the declaration channel. Check: with a withheld same-name entity at another path and without it, the restricted caller's referent outcomes, relation advisory, notices and later activation are byte-identical; a withheld page at the same path gives the entity writer's occupancy refusal, which names no path.
- [ ] 7.3 Run the agent-track acceptance with a real ordinary agent on the same turn, with no nudge, in a hookless arm and a hooked arm, one to three runs each. Check: each run's tool calls and resulting vault state are kept with its run file; a run passes when the dog is an entity of a fitting type, the ownership edge exists and a fresh-session answer uses them; the twin false-positive count is zero; results are reported per run with no aggregate.

## 8. Studio label (D6)

- [ ] 8.1 Show `relation_label` in Studio (`app.v5.js`) in its own PR under the frontend procedure. Check: Chrome DevTools inspection of the graph and relation-queue views before and after; accept payloads still send the key.

## 9. Delivery and closure

- [ ] 9.1 Run the scoped suites, then the full corpus, ruff, mypy, OpenSpec strict and the privacy gate at the completion boundary; obtain an independent review with a C4 and C6 verdict. Check: outputs and the review verdict are recorded on the PR.
- [ ] 9.2 After merge, record evidence against the close-memory-loop and agent-led vocabulary tasks in the D7 table without ticking tasks this change does not complete; synchronize the deltas and archive through `openspec archive`. Check: `openspec validate --all --strict` passes before and after the archive.
