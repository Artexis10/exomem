# Design

## Context

The programme brief asks that ordinary work turns durable referents into entities and typed edges, and that later activation uses them.
The no-nudge architecture assigns the work: deterministic sensors measure, frozen verifiers label into queues, the active conversation agent decides, and carriers deliver state on every response.
The relation audit adds that `relates_to` is honest when nothing more specific is evidenced, that specific predicates are offered cheaply, and that relation count is never a target.

Evidence pointers below are verified against `origin/main` at `42c807797`.
Revision 2 follows an adversarial critique (REQUEST_CHANGES, 2 blocking and 7 major findings): it measures before it builds, ships the smallest prompt first, and gates the declaration argument on evidence.
Revision 3 follows the recheck (REQUEST_CHANGES, 1 blocking and 5 major findings): it preregisters the pass line that closes the change, splits the shipped arm so that the receipt block must earn its place, and makes the entity revert one call.

## Owner decisions (decided 2026-10-10)

- **O1. Outcome.** Entity, category, relationship and type creation is first class and fully dynamic, and a promotion reaches all future interactions.
  The owner's complaint: "It only did when I told it to do it."
- **O2. Authority.** Agents create entity types, relation types, categories, entities and typed edges without confirmation when the new type fits a registered parent family.
  Every creation carries provenance (the originating write or episode), surfaces once in the next session's counters, and reverts in one call.
  This replaces the v1 confirm-first rule for these classes.
  Whatever survives of the opt-in v2 grants and T13 owner control (#1124) must not gate the normal owner's promotion.
- **O3. Architecture.** The active conversation agent is the only decider.
  Sensors, verifiers and carriers serve it.
  Code holds no word list or regex that decides meaning (C6), and vocabulary lives in registries (C4).
- **O4. Display.** Humans and agents see the label, for example `has_pet (a kind of owns)`, never `vault.has_pet`.
  The namespaced key stays the machine identity.
  A label is qualified only when two namespaces share it.
- **O5. Acceptance.** A rich conversational turn about a person's pet, health product and diet, replayed with no corrective nudge, yields the pet as an entity with the right type, an ownership edge to the person, and later activation in a fresh session that resolves and uses them.
  Negative twins (incidental names, one-off mentions) stay unpromoted, with a declared false-positive ceiling of zero on twins.
  The run uses the real compiler and capture pipeline, not hand-built packets.

## Goals / Non-Goals

**Goals:** a measured no-nudge baseline before any build; the smallest write-path prompt that turns a named referent into an entity; owner promotion without confirmation, with provenance, surfacing and revert; specific relations offered and writable in one call; one display label.

**Non-goals:** server-side referent detection in prose, server choice of a type or predicate, recurrence-based promotion before origin accounting, a bulk curation pass over existing vaults, and any part of this change that the measurement does not earn.

## Decisions

### 1. The decider's channel (T1)

**Evidence.**
- The compact core already tells the agent to "Wikilink named people, places, organisations, equipment and products even with no page yet" at `balanced` and `maximal` (`prominence.py:192-195`). The pet failure happened under that text.
- `capture_sweep.hints()` finds the names that a page links and that resolve to no page and no entity (`capture_sweep.py:289-340`).
  Only the 30-minute quiet block calls it (`capture_sweep.py:215`, `capture_sweep.py:625`).
- `capture_sweep.entity_candidate()` runs on every write (`semantic_writes.py:2741`) and fires only when a name goes from one linking page to two (`capture_sweep.py:343`).
  Nothing tells the agent about a name on its first page.
- Entity creation is first class: `connect_memory` `resolve-entity` and `create-entity` (`link.py:582`), with `identity_decision` for shared names (`link.py:143-159`).
- Episode `prepare` seals `create-entity` and `accept-relation` steps, and `resume` runs them only when `EXOMEM_EPISODE_WORKFLOW` is set (`episode_workflow.py:1-45`, `curation.py:44-53`).
- The server does not detect a first mention in prose, by design (`capture_sweep.py:301-303`).

#### 1A. The `undeclared_referents` receipt block (kept by measurement)

1. Every committed durable page write carries an `undeclared_referents` block in its receipt.
   It lists at most 3 names that this write newly wikilinks, that resolve to no page and no active entity in the writer's own view, and that no declaration covers.
   "Newly" uses the pre-write page state, as `entity_candidate` does.
2. Each name carries the `create-entity` route.
3. A name that the same response's `entity_candidate` block carries is omitted, so one name never gets two prompts.
   The block is the 0→1 step; `entity_candidate` stays the 1→2 step.
4. It reuses the `hints()` logic and the preflight's corpus context: no vault walk and no model call.
   It reads wikilink markup, not prose, so C6 holds.
5. The existing write-advisory fingerprints dismiss a name, and the family `undeclared_referents` can be quieted.
   A dismissal also quiets that name's later `entity_candidate` block, so a name that the agent rejected once is not prompted again at its second page.
6. It is withheld when `structural_suggestions` is `off`, exactly as `entity_candidate` is.
   It is also withheld when `proactive_capture` resolves to `off`, as it does at `light`: the block asks the agent to create something, which `light` does not invite.
7. A name that pages of one mutation batch newly link counts once for the batch, as for `entity_candidate`.
8. A name that matches only a page the writer may not see is listed exactly as a name with no page.
9. The compact response projection carries the block beside `entity_candidate` (`mutation_terminal.py:1812`, `mutation_terminal.py:1965-1967`, `mutation_terminal.py:1973`).
   A hookless client reads only that projection, and in-process tests skip MCP egress, so the block's tests read it from a hookless MCP response.

The block makes the agent's own markup into a prompt at the moment the agent holds the context.
It costs no tool-schema bytes, because the receipt is not part of the input schema.
It ships only if the measurement shows that it earns its place (decision 8, keep rule).

#### 1B. `referents` declarations (gated)

The declaration argument is built only if the shipped arm misses the pass line of decision 8, and it ships only if arm C then beats that arm.
Otherwise it stays specified here and unbuilt, and its requirements leave the spec deltas before archive.

1. One optional `referents` argument with one shared schema, first on `remember` and `observe_memory` only.
   `edit_memory` and `capture_source` follow only on evidence (R5).
2. A declaration list holds at most 8 entries with `name`, `evidence`, and optional `type`, `ref`, `summary`, `aliases`, `relations` (at most 3, each `{predicate, subject}` or `{predicate, object}`) and `identity_decision`.
3. `evidence` matches the committed text after the writer's Unicode normalization, including wikilink display text.
   For `edit_memory`, it matches only the text that the edit adds.
   A span that does not match gives that declaration the outcome `evidence_not_found`; it never refuses the primary write.
   Only a malformed argument shape refuses the call before any write.
4. After the primary write commits, each declaration runs through existing leaves and gets one outcome:
   - `reused`: exactly one visible match.
   - `created`: no match, a registered `type` and a `summary`; the `create-entity` leaf runs.
   - `needs_decision`: a shared name; candidates and `candidate_fingerprint`.
   - `needs_type`: the declaration carries no `type`; the outcome returns the `create-entity` route at once, and nothing stays pending.
   - `type_unknown`: the declared type is not registered; the declaration stays `pending` on the write's operation, and the outcome names the one completion call, `connect_memory(operation="complete-referents", ref=<operation ref>)`, to run after the type save.
   - `needs_create`: the caller is restricted. Until R4 ships, a restricted caller's declaration never reaches `created`; it always gets the `create-entity` route.
   - `unavailable`: a limited owner's `type` resolution needs withheld private registry definitions.
   - `unresolved`: the `ref` is not visible; a withheld ref gives the same outcome as a missing one.
   - `evidence_not_found`: see item 3.
5. A pending declaration appears in due-state (decision 5) until it completes or the agent dismisses it; its delivery never settles it.
6. Relations run after both endpoints exist, through typed `connections` or `add-relation` (decision 3).
7. The write's operation identity binds every referent leaf; an identical retry replays outcomes and creates nothing twice.

**Budget.** Measured with `scripts/measure-tool-schema-bytes.py` at `2b9500f06`:

| Tool | Bytes | Ceiling (`tests/test_tool_schema_budget.py:26-58`) |
|---|---|---|
| Surface total | 89,018 | 90,000 |
| `edit_memory` | 6,714 | 6,725 |
| `remember` | 5,195 | 5,200 |
| `observe_memory` | 3,847 | 3,850 |
| `capture_source` | 3,443 | 3,475 |
| `connect_memory` | 4,292 | 4,300 |

`referents` adds about 844 bytes per tool.
On `remember` and `observe_memory` it needs both per-tool ceilings raised by about 850 bytes and the surface total raised by about 1,700 bytes, plus the `complete-referents` operation on `connect_memory`.
The reason for the raise, if arm C beats the shipped B arm run beside it, is that measured difference; without it the raise is not justified.
The core capture line for declarations (at most 160 bytes, served at `balanced` and `maximal` only) also ships only with the gate.

### 2. Authority: owner promotion is proactive capture (O2)

**Evidence.**
- On 2026-10-07 the owner ruled that agents promote and humans revert: a v1 owner's registry save takes effect at once (`add-vocabulary-registries` design decision 7; `activate-agent-led-vocabulary-evolution` design decision 7).
- Ruling R1 of 2026-09-28 put additive entity creation under `proactive_capture` (close-memory-loop design, "Identity batch 1 rulings").
- The served contract still requires confirmation, or leaves an owner promotion unclassified, in nine places:
  - `commands.py:1313-1317`: "Decision not permission; v1 writers retain confirmation";
  - `commands.py:1092`: the structured entity-lifecycle field `"relation_outside_curation": "link_acceptance"`;
  - `prominence.py:202`: "Missing schema: structural_suggestions/restructure_execution; relations: link_acceptance";
  - `prominence.py:384-385` and `prominence.py:419-420`: "an affiliation relation requires link_acceptance";
  - scaffold `SKILL.md:178`: `link_acceptance` covers "accepting a suggested relation" with no owner-authored edge row;
  - scaffold `references/operations.md:38` and `references/engagement.md:63`: "affiliation relations require `link_acceptance`";
  - scaffold `workflow-skills/exomem-capture/SKILL.md:51`: "affiliation relations use `link_acceptance`";
  - `envelope.py:124-129`: `CONFIRM_REQUIRED` names additive entity creation but neither the owner's registry saves and typed edges nor edge removal.
    `DECIDER_PROTOCOL` (`envelope.py:137-147`) tells the agent that "an unclassified action has no envelope cell and therefore no authority", so an action that the served text does not classify stays without authority.
- The canonical ceiling requirement still lists entity creation under `restructure_execution`; the close-memory-loop delta moves it to `proactive_capture`.
- The `complete-recurring-entity-lifecycle` envelope delta classed an unknown-kind type save as confirm-required.
- v2 is an explicit opt-in vault activation with no default grants (`activate-agent-led-vocabulary-evolution` design decision 4). T13 is open PR #1124.

**Decision.**
1. O2 is the founder ratification that changes the ceilings.
   The `delegation-envelope` delta is a MODIFIED "Hard authority ceilings bound every action class".
   It carries the close-memory-loop changes and adds: when the caller resolves to the owner and v2 is not activated, an entity-type or category save, a relation-extension save, an entity creation and an agent-authored typed edge are `proactive_capture`.
2. Provenance, surfacing and revert (decision 5) replace the confirmation question.
3. Family fit: a relation extension fits by construction (close-memory-loop task 5.12); a parentless entity type promotes too (R1).
4. `link_acceptance` keeps covering acceptance of a relation that the server's queue suggested (R3).
5. `restructure_execution` keeps merge, supersession, deletion, meaning changes, alias edits on existing entries and deprecation, and gains a scoped edge removal:
   - a `remove-relation` call;
   - a removal of an edge from a page that the current write does not author.

   A user's revert request is the confirmation of either.
   An `edit_memory` call that drops a `## Relations` bullet from the page it writes is part of that write, not an edge removal that asks.
   Replacing a generic edge in place with a specific predicate (decision 3, item 7) is an authored typed edge, not a removal.
6. A resolved non-owner's registry save stays a pending item; its creations stay inside its write scope.
   A limited owner's save that needs withheld private registry definitions reports `unavailable`, never a success.
7. In a v2-activated vault every promotion passes the v2 effect classifier. No upgrade, migration or default activates v2, and T13 routes no v1 owner promotion to approval.
8. Task 1.1 rewrites all nine served texts.
   `CONFIRM_REQUIRED` names the owner's promotions as `proactive_capture` and the scoped edge removal as confirm-required, and `relation_outside_curation` names the class of an owner-authored edge.
9. The served envelope also carries `promotion_classes`, a structured map from each promotion action to the class that governs it for the caller: entity creation, typed edge, registry save, queue acceptance, edge removal and promotion revert.
   The keys are the product's own closed action kinds, which this change defines and code implements, and the values are the closed `ACTION_CLASSES`; neither holds vault data, so C4 holds.
   For a resolved non-owner, a registry save maps to `structural_suggestions`, because it becomes a pending item for the owner; a v2-activated vault serves its grant rule instead of the map.
   The map exists so that a test can check class coverage on the payload rather than on phrases.
   Its bytes count against the compact budget (`tests/test_bootstrap_compact_budget.py:37`), and the shorter rewrite of `CONFIRM_REQUIRED` pays for them where it can.
10. This change amends now:
    - the `complete-recurring-entity-lifecycle` envelope delta, so an owner's type save follows `proactive_capture` and only a queue-suggested relation keeps `link_acceptance`;
    - the `capture-durable-personal-baselines` envelope delta, whose affiliation scenario now reads as queue acceptance;
    - the `write-time-identity-candidates` requirement "The write that creates a candidate delivers it", from "The server SHALL never create an Entity" to "Detection SHALL never create an Entity".

### 3. Specific relations (T2)

**Evidence.**
- `link.py:391` renders every `create-entity` connection as `relates_to`.
- `source_kinds` and `target_kinds` scope node kinds, not entity families (`relation_registry.py:163-168`, `relation_queue.py:119`, `epistemic_graph.py:10762-10763`).
- Code compares the `relates_to` key at `mutation_terminal.py:1402`, `vocabulary_signals.py:71` and `relation_census.py:60` (C4 debt).
- The compact envelope already carries `relation_advisory` (`mutation_terminal.py:1975`).
- The generic save delta knows `upsert`, `alias` and `deprecate` only (`vocabulary/registry.py:54`); relation deprecation needs a replacement (`relation_registry.py:1237`).

**Decision.**
1. `generic: true` marks the core pack's one generic relation, and `endpoints: {subject, object}` lists entity families or the closed token `any_entity`. Both are registry data; extensions inherit `endpoints` but not `generic`.
2. R2 adds a symmetric kinship relation and an affiliation relation to the core pack, with endpoints.
3. `create-entity` `connections` accept `{target, relation}` items beside strings; a string keeps the generic relation.
4. `connect_memory(operation="add-relation", path, requested_relation, target, expected_hash, why)` appends one bullet under the subject page's `## Relations`, idempotent by `exists`, hash-guarded, with a log entry.
5. `connect_memory(operation="remove-relation", path, requested_relation, target, why)` removes the bullets of that triple.
   It is keyed by subject, predicate and target, takes no hash, and returns `absent` when no such edge exists. It is the edge revert.
   Both leaves use existing `connect_memory` parameters and are curation step kinds that compensate each other.
6. Endpoints never refuse a write; a mismatch adds a non-blocking `endpoint_mismatch` finding.
7. When a committed write authors the generic relation between two entity pages, `relation_advisory` carries at most 4 `specific_options`.
   Each option's route replaces the generic bullet in place with one hash-guarded `edit_memory` `replace_string`, so accepting an option never leaves a second edge.
   A predicate with no endpoints is never offered; dismissal uses the write-advisory fingerprints.

**Budget.** `connect_memory` is 8 bytes under its ceiling. Two operation names and the `connections` item union need a measured raise of that ceiling, stated with this reason in task 2.4.

### 4. Typed activation (T3, conditional)

This decision is scheduled only if the shipped arm still misses the pass line of decision 8 after decision 1B has had its turn, and its trigger holds: an owner-anchored question fails while its entity and edge exist.

**Evidence.**
- The built-in profiles are a dict in code (`traversal_profiles.py:143-180`), which is C4 debt.
- Activation applies `GRAPH_TRAVERSAL_PROFILE = "epistemic"` to every resolved anchor (`working_set.py:79`), and `_graph_neighbours` passes no `keep` (`working_set.py:1199-1207`).
- `graph_context` fetches neighbour rows with `relation_filter=set()` (`epistemic_graph.py:8842-8847`) up to `(max_nodes + max_edges) × 4` = 256 rows for activation (`epistemic_graph.py:79`, `epistemic_graph.py:11576-11578`), ordered by `edge_key`, before the profile filter. A person with more than 256 incident edges loses typed edges to truncation.
- `epistemic` excludes the ownership, membership and location families (`traversal_profiles.py:149-157`, `traversal_profiles.py:252-262`).

**Decision.**
1. Move the built-in profiles into `packs/core/traversal-profiles.yaml` with a parity check.
2. Define `entity` as `extends: epistemic` plus the entity families: ownership, membership, location, operation, supply, production, composition, use, entity, and the R2 kinship and affiliation families.
3. A pack profile declares `activation_anchor_kinds` from the closed `ANCHOR_KINDS` set (`working_set_index.py:132`); no profile key remains in activation code.
4. Pass the profile's allowed relation keys into the `_neighbor_edges` SQL filter, so truncation applies only to edges that the profile can use.
5. Pass the reader's `keep` from `_graph_neighbours` into `graph_context`.
6. Serve a typed neighbour that the `entity` profile reaches as a `pointers[]` entry whose `why` names the relation labels on the path; the tests assert on `pointers[]`.
7. Hold the owner's activation bar: under 1 second at p95, warm. Report each latency pair as a difference with its interval.

### 5. Surfacing, provenance and revert (T4)

**Evidence.**
- The due-state carrier serves categories on bootstrap, mutating responses and recall (`due_state.py:107-123`), with emission keyed by session, audience and vault (`due_state.py:200-205`). CLI and stdio fall back to one process key (`due_state.py:219-222`).
- The first-surfaced ledger stamps delivered items (`review_state.py:1145-1215`, `attention.py:843`).
- Per-edge provenance today is the authoring write's log entry (`tests/test_operator_site_cohort.py:70-103`).
- `manage_memory_file` delete moves a page to `_trash` (`commands.py:5894-5930`); the curation `delete` step takes `expected_dead_inbound` (`curation.py:203-213`).
- A due-state entry carries a review ref and a fingerprint (`due_state.py:478-486`), and `triage_memory` decides one item by `ref`, `action`, `why` and `expected_fingerprint` (`commands.py:9328-9350`).

**Decision.**
1. Provenance: an entity creation or typed edge writes a log entry on its page that names the originating write's path, operation id and episode key when present; a registry save keeps its history header and reason.
   `create-entity` takes the originating write's ref in its existing `ref` argument, and the `undeclared_referents` route fills it, so no new parameter is needed.
   An origin ref that the caller cannot read, withheld or missing alike, records the origin as unknown and never refuses the creation.
2. The due-state category `recent_promotions` holds one entry per registry addition, entity creation and typed edge between two entity pages, whichever leaf wrote it.
   A bulk writer, such as adoption or import, adds one entry per originating write, not one per page.
   A parentless type's entry carries `new_family` and is served first (R1).
   Pending declarations, if decision 1B ships, are entries with their completion call.
3. The creating session never counts its own entry.
   A promotion's entry settles at its first delivery to an interactive conversation session other than the creator's.
   A hook process or a CLI or REST call never settles it. A stdio MCP session is a conversation and can settle it.
   The server may also exclude a delegated agent lane once a wire field identifies one; no such field exists today (Open items).
   A pending declaration's entry settles only when it completes or the agent dismisses it, never on delivery.
   A revert or a dismissal settles any entry. A withheld promotion adds nothing to a restricted audience's count.
4. Every entry reverts by one call keyed by its ref: `triage_memory(ref=<entry ref>, action="revert")`.
   The entries already live in the review store that `triage_memory` decides, so the call adds one action value and no parameter.
   The server runs the effect that fits the entry:
   - A typed edge: the `remove-relation` step for that triple.
   - An entity creation: the server seals and applies, in that one call, a curation plan whose steps remove the promotion's own edges and then trash the entity.
     The entry's item context lists every other inbound link as a dependant, and its fingerprint covers that list.
     A link from the originating write is not a dependant: after the revert it is an unresolved wikilink again, as before the promotion.
     When dependants exist, the call refuses and returns them, unless it carries the entry's current fingerprint after the user named them.
     A changed dependant list changes the fingerprint, so the call refuses again.
   - A registry addition: one key.
     With no dependants the key is removed through a new `remove` delta verb.
     The verb is allowed only in a registry whose adapter declares a usage check (today entity types, relations and semantic categories) and only for a vault-added entry that nothing uses; every other registry refuses it.
     With dependants the key is deprecated (a relation to its parent), and the result lists the dependants.
     A revert never restores an older version and never rolls back a later save.
5. A revert is `restructure_execution`. It runs on the user's request, which is its confirmation; the entry's item context is its preview.
   History and logs keep both the promotion and its revert.
   The leaves stay callable on their own: `remove-relation` for any edge, and a curation plan for any wider undo.

### 6. One display label (T5)

**Evidence.**
- `RelationDefinition` has no `label` (`relation_registry.py:55-71`), but the generic registry entry does (`vocabulary/registry.py:67-79`).
- A new extension already stores its clean authoring label as its first alias (`relation_registry.py:340-342`), and `extension_key_for_label` adds the `vault.` namespace (`relation_registry.py:571-573`).
- Keys are emitted verbatim by `relation_registry.py:76`, `relation_vocabulary.py:214-229`, `epistemic_graph.py:328`, `find_types.py:288` and `find_types.py:359`, `relation_census.py`, the relation advisory in `mutation_terminal.py`, and Studio `app.v5.js:307,450,708`.

**Decision.**
1. A relation entry carries `label` as registry data; absent, a core relation's label is its key and an extension's label is the segment after its namespace.
2. One formatter, `relation_registry.display_label(registry, key)`, renders a core relation as its label, an extension as `<label> (a kind of <parent label>)`, a shared label with its namespace added, and a deprecated entry with `(deprecated)` added.
3. Every producer adds `relation_label` beside its existing key field; prose renderings use the label; every existing key field keeps the key.
4. The title-first presentation rule extends to relations: say the label, and pass the key, label or alias in calls.
5. Studio is a separate task with its own PR under the frontend procedure.

### 7. Order against the programme (T6)

The measurement comes first: the case set, its answer key and the baseline on `main` exist before any part of this change merges.
Then the smallest prompt ships, and each larger mechanism is built only when the shipped arm misses the preregistered pass line of decision 8.

This change does not wait for close-memory-loop task 4.3.
Origin accounting is needed only for recurrence-based promotion, where the server must know that two mentions come from independent originals.
A first-mention prompt and an agent's declaration need no origin count: the decider has the evidence.

| Existing task | Relation to this change |
| --- | --- |
| add-context-activation-benchmark 1.6 | Overlaps. Task 3.2 here is a capture-to-activation replay for referents only; it reuses 1.6's harness when 1.6 lands first. 1.6 keeps fan-out and interrupted resume. |
| add-context-activation-benchmark 4.3 | Overlaps. Group 4 here is a referent instance of 4.3's rich-turn journey and uses its observation format. 4.3 stays open for multiple topics. |
| f27 agent-track driver (`benchmarks/epistemic/journeys/f27_replay.py`) | Reused for every arm of group 4. |
| close-memory-loop 3.4 | Related. The core line that 3.4 owns stays; a declaration line joins it only if decision 1B ships. |
| close-memory-loop 4.3, 4.3a-c | Not a dependency. |
| close-memory-loop 4.5 | Related. `entity_candidate` stays the 1→2 step and still switches to origins after 4.3. |
| close-memory-loop 5.3 | Narrowed. The owner's promotion no longer depends on it. |
| close-memory-loop 6.1 | Contributes. Group 4 gives the Claude Code rich-turn and fresh-session pair, hookless and hooked. |
| close-memory-loop 6.2 | Independent. ChatGPT and other clients stay there; the hookless Claude arm here is only a proxy for them. |
| close-memory-loop 6.3 | Contributes cross-kind topology tests if decision 4 runs. |
| close-memory-loop 6.4 | Depends on this change for the private pet-and-owner replay. |
| close-memory-loop 6.6 | Reuses the task 3.2 replay harness. |
| close-memory-loop 6.17, 6.18 | Independent; decision 4 keeps their tests and latency bound. |
| close-memory-loop 9.4 | Its `delegation-envelope` MODIFIED block must be refreshed against this change's block, whichever archives second. |
| agent-led vocabulary 2.1 | Complements. The offer needs no origins. |
| agent-led vocabulary 4.1, 4.3, 5.1, 5.5 | v2 only; off the owner's path. |
| agent-led vocabulary 7.1-7.4 | Superseded for the owner's vault by decision 2. |
| add-vocabulary-registries 5.1 (S5), 9.1 (S9) | Compatible; the offer shares S9's advisory slot. |
| capture-durable-personal-baselines 5.6 | Its envelope delta is amended in this change: affiliation acceptance means queue acceptance (R3), and an owner-authored affiliation edge follows `proactive_capture`. Task 1.1 rewrites the served copy. |
| complete-recurring-entity-lifecycle 7.5 | Its envelope delta is amended in this change. |

### 8. Proof plan (T7)

The workflow that this change alters is ordinary capture followed by later activation.
The highest-level check is the agent-track measurement of group 4, and it runs first.

**Measurement.**
1. A case-authoring lane, never an implementation lane, files a new §7 amendment in `benchmarks/epistemic/PREREGISTRATION.md`: the next free family (`f33` on this base) as sequence 7, following the f27 entry's rules (`PREREGISTRATION.md:323-339`).
   The same lane authors the case set and its answer key from source truth, never from a measured session.
   It records their SHA-256 digests in the amendment before groups 2 and 3 are built.
   Implementation lanes never read the cases, and every arm run checks the digests.
2. The owner is asked to acknowledge the amendment as soon as it lands.
   Until he does, every comparative result and the gate of task 5.2 read as pending (`benchmarks/epistemic/amendments.py:95-107`).
3. Cases:
   - Positives cover the owner's list: pet and owner, person and kin, equipment and its consumable, supplier and operator, community and membership, and a sparse existing entity.
   - Each positive has a negative twin that differs by durability, not by mention count: the same name mentioned as often, but as a passing or one-off thing.
   - One owner-anchored question per positive asks about the referent in a fresh session by naming the person.
   - The key states each answer fact as an exact value, such as a name, a number or a date.
   - No case shares a name or wording with the examples in this change's artifacts, nor a type pair or relation pair with the CI replay of task 3.2.
4. The pass line, from the owner's ruling of 2026-10-10, is stated in the amendment before the baseline runs. The shipped arm meets it when:
   - the pet-and-owner positive passes in both client shapes;
   - at least 5 of the 6 positives pass, counted in each client shape;
   - no twin case fails, so there are zero twin false positives.

   A case passes when it passes 2 of its 3 repeats. The report also gives every repeat's twin false positives.
5. A positive passes a repeat when three things hold:
   - the entity exists with a type that fits the answer key;
   - the answer-key edge exists;
   - the fresh-session answer uses them.

   "Uses" is decided deterministically: the activation packet serves the entity's ref, and the answer contains the key's exact value.
   Where that cannot decide, a blind judge from another model family reads the answer against the key, with the same reference view for every arm.
   A twin passes a repeat when no entity, edge or notice exists for its name.
6. Every arm runs with the f27 driver, hookless and hooked, 3 repeats per case per client shape, on the same authored turns.
   Its manifest pins the client version, the model, the product version and the prominence level (`PREREGISTRATION.md:333`). The arms:
   - baseline: `main` at the base revision;
   - arm B1: the authority text and the typed-edge leaves (groups 1 and 2);
   - arm B2: arm B1 plus the receipt block (group 3);
   - arm C: the shipped B arm plus `referents` (group 5), run only on escalation, with the shipped B arm run again beside it under the same pins.

   The hookless Claude arm is a proxy for a hookless client such as ChatGPT, not a ChatGPT result; ChatGPT stays with close-memory-loop task 6.2.
7. Arm X beats arm Y when X passes at least one positive case that Y fails, fails no case that Y passes, and has zero twin false positives.
8. The keep rule for the receipt block: the block ships, and B2 is the shipped arm, only if B2 beats B1 or the run files show the agent acting on a listed name through its route.
   Otherwise B1 is the shipped arm, and group 3 leaves the branch with its requirement.
   For B2 the report gives the block emissions per write (the share of writes that carry a block, and names per block) and the shares of listed names acted on, dismissed and left alone.
9. Escalation: when the shipped arm misses the pass line, the change stays open, and the next mechanism runs in this order:
   1. decision 1B: build `referents` and run arm C; C becomes the shipped arm only if it beats the B arm run beside it;
   2. decision 4, when its trigger holds;
   3. a report to the owner with the per-case tables.

   Tasks 9.1 and 9.2 cannot close the change until the shipped arm meets the pass line or the owner rules.
10. The trigger for decision 4: at least one owner-anchored question fails in the shipped arm while its entity and edge exist.

**Boundary tests.** Each names the failure that only it catches.
- CI replay (task 3.2): the shipped flow through the real doors, publication and a fresh `activate_context`, with twins. It catches a break between prompt, creation, publication and activation.
  Its invented scenario differs from every f33 case: a person keeps a named sailing boat at a named harbour.
  If decision 1B ships, the replay starts from the declaration, with no pre-saved type.
- Served text (task 9.1): the case-authoring lane checks that no case wording appears in a changed core line, tool description or scaffold text. Only a lane that holds the cases can check this.
- Authority (task 1.2): a non-owner or v2 vault cannot use the new routes to bypass its gate.
- Disclosure twin (task 3.3): a withheld same-name entity at another path, and at the same path, gives a restricted caller the same receipt block, advisory and notices as a vault without it, read through MCP egress. The existing `create-entity` same-path refusal (`link.py:232-248`) stays reported debt until R4.
- Revert (task 6.2): each entry reverts with one `triage_memory` call and leaves history; dependants block an entity revert until the user names them.
- Surfacing (task 6.1): an entry is counted once, in the right session, and a hook never settles it.

## Risks / Trade-offs

- The receipt block could become noise → at most 3 names, only newly linked unpaged names, fingerprint dismissal and family quiet, and the keep rule drops it unless it earns its place.
- The measurement costs agent runs → the baseline, B1 and B2 take 216 runs (3 arms × 12 cases × 2 client shapes × 3 repeats), and escalation adds 144; all run on the subscription.
- `referents` could be built and then dropped → it is built only when the shipped arm misses the pass line, and its budget raise is never taken without the gate.
- A revert can strand dependants → they block the revert until the user names them.

## Migration Plan

1. The case-authoring lane files the amendment and freezes the cases; run the baseline.
2. Ship the authority text and typed-edge leaves, build the receipt block, and run arms B1 and B2; apply the keep rule.
3. When the shipped arm misses the pass line, escalate in the order of decision 8.
4. Ship surfacing, revert and labels.
5. Rollback: older releases ignore the new optional registry fields, the receipt block and the new due-state category; pages keep their bytes.

## Rulings (owner delegate, 2026-10-10)

- **R1. A parentless entity type** promotes at once. Its notice carries `new_family`, it is surfaced first in the next session, and it reverts in one call. Asking first would put a question in the O5 path.
- **R2. Kinship and affiliation:** this change adds a symmetric kinship relation and an affiliation relation to the core pack, as pack data. Task 2.2.
- **R3. Queue acceptance:** `link_acceptance` keeps its confirm ceiling for queue suggestions in this slice. The agent's own declared edges do not need it.
- **R4. Path occupancy for restricted writers:** suffixed filenames for restricted writers, delivered with the connector-ceiling work of `add-governed-vault-consolidation`.
- **R5. More writers:** measure the four writers first. `record_memory`, `replace_memory` and `episode_memory record` follow on evidence.

## Open items

- A delegated lane has no declared role on the wire today. Decision 5 lets the server exclude a lane once a field identifies one; until then, only hook processes and CLI or REST calls are excluded from settling an entry.
- R5 named four writers; the budget in decision 1B starts `referents` on two. If the gate passes, `edit_memory` and `capture_source` need their own measurement and budget raise.
