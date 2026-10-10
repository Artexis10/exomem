# Tasks

Each task names its check.
Run the scoped suites a task touches during development, and the full corpus at the completion boundary (task 9.1).
Design decisions are cited as D1 to D8.
Tasks 4.1 and 4.2 run before any part of this change merges; they may run beside groups 1 to 3.

## 1. Authority text (D2)

- [ ] 1.1 Rewrite the six served texts that require confirmation for owner promotions: `commands.py:1313-1317`, `prominence.py:202`, `prominence.py:384-385`, `prominence.py:419-420`, scaffold `SKILL.md:178`, `references/operations.md:38` and `references/engagement.md:63`; then regenerate the plugin and cloud copies. Check: a new core test fails on the base while any of the six phrases is served or shipped, and passes after; a v2-activated fixture still serves the v2 rule; the no-leak, skill-contract and plugin-sync tests pass.
- [ ] 1.2 Pin the D2 classes at their leaves. Check: an owner's type save, relation-extension save and typed edge commit with no confirmation argument; a resolved non-owner's relation save stays a pending item; a limited owner's save that needs withheld registry definitions reports `unavailable`; a v2-activated vault without a grant refuses an entity creation; an upgrade of a v1 fixture leaves it v1.

## 2. Typed-edge leaves and relation data (D3)

- [ ] 2.1 Add `generic` and `endpoints` to the relation adapter and the core pack, with findings for an unknown family, an extension side outside its parent's, and other than one active generic relation. Check: save and history round trips with both fields, endpoint inheritance, a narrower and a wider extension, and parity of every existing key.
- [ ] 2.2 Add a symmetric kinship relation and an affiliation relation to the core pack with families and endpoints (R2). Check: the parity test lists both; a vault extension can parent on each.
- [ ] 2.3 Replace the `relates_to` key comparisons on the touched paths (`mutation_terminal.py:1402`, `vocabulary_signals.py:71`, `relation_census.py:60`, `link.py:391`) with the `generic` attribute. Check: the semgrep C6 gate passes; the advisory, signal and census tests pass unchanged; the PR lists the remaining literals as C4 debt.
- [ ] 2.4 Accept `{target, relation}` items in `create-entity` `connections`, and raise the `connect_memory` schema ceiling by the measured amount for this task and tasks 2.5 and 2.6. Check: a typed item renders its predicate; a string renders the generic relation; an unknown predicate refuses and writes nothing; MCP, CLI and REST accept the same shape; `tests/test_tool_schema_budget.py` passes with the raise and its reason recorded beside the ceiling.
- [ ] 2.5 Add the `add-relation` operation and curation step kind. Check: one bullet and one log entry per call; a repeat returns `exists` with unchanged bytes; a stale hash refuses; an unregistered predicate refuses with the propose route; withheld and missing targets refuse byte-identically; the operation is classified in the egress selector table and the credential and owner-control matrix.
- [ ] 2.6 Add the `remove-relation` operation and curation step kind, keyed by subject, predicate and target, with no hash. Check: it removes exactly the bullets of that triple; a repeat returns `absent`; an unrelated concurrent edit to the page survives; `add-relation` and `remove-relation` compensate each other in a curation plan; withheld and missing subjects refuse byte-identically.
- [ ] 2.7 Add `specific_options` to `relation_advisory`, each with an in-place replacement route. Check: a person-to-animal generic edge written through `add-relation`, a `## Relations` bullet and a string connection each offers the fitting predicate; running the route leaves one edge, not two; a note-to-entity generic edge offers nothing; a predicate without endpoints is never offered; at most 4 options; a dismissal suppresses the offer; no write refuses.

## 3. Receipt block (D1A)

- [ ] 3.1 Add `undeclared_referents` to every committed durable page write's receipt, reusing `capture_sweep.hints()` beside `entity_candidate`. Check: a write that newly links an unpaged name lists it with the `create-entity` route; at most 3 names; a name in the same response's `entity_candidate` block is omitted; a name that the page already linked is omitted; a link to an existing entity lists nothing; the block is absent when `structural_suggestions` is `off`, after a fingerprint dismissal, and when the family is quiet; a name matching only a withheld page is listed exactly as an absent one; the write path does no vault walk.
- [ ] 3.2 Build the CI replay of the shipped flow on a synthetic product-shaped corpus: an invented person entity and earlier notes; a scripted decider that writes the rich turn about the person's dog, its supplement and its diet with wikilinks, follows the receipt block, saves one entity type and authors the ownership edge; the real publication pipeline; a fresh-session `activate_context`. Check: the dog is an entity of the saved type; the person-to-dog ownership edge is in the graph; the fresh packet resolves the dog and serves the owner context; the incidental-name and one-off twins produce no entity, edge or notice; a per-case table is recorded. If task 5.2 ships `referents`, the replay also starts from a declaration with no pre-saved type.
- [ ] 3.3 Add the disclosure twin. Check: with a withheld same-name entity at another path, at the same path, and absent, a restricted caller gets byte-identical receipt blocks, relation advisories, notices and later activation; the existing `create-entity` same-path refusal is listed as debt until R4.

## 4. Measurement (D8)

- [ ] 4.1 File the §7 amendment for the next free family (`f33` on this base) and author the case set and answer key from source truth: six positives (pet and owner, person and kin, equipment and its consumable, supplier and operator, community and membership, a sparse existing entity), one durability-matched negative twin each, and one owner-anchored fresh-session question each. Check: the amendment states the arms, the per-case pass rule and the D1B gate; no answer-key value comes from a measured session; the receipt is submitted for founder acknowledgment.
- [ ] 4.2 Run the baseline on `main` at the base revision with the f27 driver, hookless and hooked, one run per case per client shape. Check: each run's transcript, tool calls and vault snapshot are kept with its run file; harness faults are recorded as blocked, never as results; the per-case table is recorded.
- [ ] 4.3 Run arm A on group 1 only, and arm B on groups 1 to 3, on the same authored turns. Check: per-case tables beside the baseline; twin false positives reported per arm; the owner-anchored questions that fail while their entity and edge exist are listed, which is the D4 trigger.

## 5. Declarations (D1B, gated)

- [ ] 5.1 Build `referents` on the branch for `remember` and `observe_memory` only: pure-logic tests first for shapes and the outcomes `reused`, `created`, `needs_decision`, `needs_type`, `type_unknown`, `needs_create`, `unavailable`, `unresolved` and `evidence_not_found`; the pending state and the `complete-referents` call; the core declaration line at `balanced` and `maximal` only. Check: the tests fail first, then pass; a wrong evidence span never refuses the primary write; spans match wikilink display text and, for edits, only the added text; a restricted caller never reaches `created`; the measured schema bytes are recorded.
- [ ] 5.2 Run arm C on the same turns and apply the D1B gate. Check: when C passes at least one positive that B fails, fails none that B passes and keeps twin false positives at zero, keep `referents`, raise the two per-tool ceilings and the surface total with the measured C-over-B difference as the reason, and record the same evidence against the gate; otherwise remove `referents` from the branch and its requirements from the spec deltas, and record the result.

## 6. Surfacing and revert (D5)

- [ ] 6.1 Add the `recent_promotions` due-state category. Check: an entry from session A is absent in A, counted in B and settled after B's delivery; a hook process and a CLI or REST call never settle it; a bulk import adds one entry per originating write; a parentless type's entry carries `new_family` and is served first (R1); a revert and a dismissal settle it; a quiet family is not counted; a withheld promotion adds nothing to a restricted audience's count; pending declarations appear only if task 5.2 kept them.
- [ ] 6.2 Add the revert routes and the registry `remove` delta verb. Check: an edge reverts with one `remove-relation` call; an entity reverts with one curation plan that removes its own edges, trashes the page and lists other inbound links as dependants; an unused vault-added type is removed with one save; a used type or relation is deprecated (a relation to its parent) with its dependants listed; no revert restores an older registry version or drops a later save; history and logs keep both records.

## 7. Labels (D6)

- [ ] 7.1 Add the relation `label`, `relation_registry.display_label`, and `relation_label` from every producer named in D6. Check: one test per producer through its public door asserts the label of a core relation, of an extension (`<label> (a kind of <parent label>)`) and of a shared label with its namespace; schema-fidelity tests show every existing key field unchanged.
- [ ] 7.2 Show `relation_label` in Studio (`app.v5.js`) in its own PR under the frontend procedure. Check: Chrome DevTools inspection of the graph and relation-queue views before and after; accept payloads still send the key.

## 8. Typed activation (D4, only on the task 4.3 trigger)

- [ ] 8.1 Move the built-in traversal profiles into `packs/core/traversal-profiles.yaml` with a parity check, then delete the code dict and the parity check. Check: graph-context output is identical for every former built-in on the existing graph fixtures.
- [ ] 8.2 Add `entity` as `extends: epistemic` plus the entity families, select profiles by `activation_anchor_kinds`, pass the profile's relation keys into the `_neighbor_edges` SQL filter and the reader's `keep` into `graph_context`, and serve reached typed neighbours as `pointers[]`. Check: a person with more than 256 incident edges still reaches the owned entity; the invented-name test finds the reached entity in `pointers[]`; a hub anchor's packet is unchanged; a restricted reader's expansion never routes through a withheld page; the failing owner-anchored questions from task 4.3 pass on rerun; warm activation stays under 1 second at p95, and three matched latency pairs are reported as differences with intervals.

## 9. Delivery and closure

- [ ] 9.1 Run the scoped suites, then the full corpus, ruff, mypy, OpenSpec strict and the privacy gate at the completion boundary; obtain an independent review with a C4 and C6 verdict. Check: outputs and the review verdict are recorded on the PR.
- [ ] 9.2 After merge, record evidence against the tasks in the D7 table without ticking tasks this change does not complete; drop the requirements of any gated decision that did not ship; synchronize the deltas and archive through `openspec archive`. Check: `openspec validate --all --strict` passes before and after the archive.
