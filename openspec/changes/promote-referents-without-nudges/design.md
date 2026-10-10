# Design

## Context

The programme brief asks that ordinary work turns durable referents into entities and typed edges, and that later activation uses them.
The no-nudge architecture assigns the work: deterministic sensors measure, frozen verifiers label into queues, the active conversation agent decides, and carriers deliver state on every response.
The relation audit adds that `relates_to` is honest when nothing more specific is evidenced, that specific predicates are offered cheaply, and that relation count is never a target.

Evidence pointers below are verified against `origin/main` at `42c807797`.
Revision 2 follows an adversarial critique (REQUEST_CHANGES, 2 blocking and 7 major findings): it measures before it builds, ships the smallest prompt first, and gates the declaration argument on evidence.

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

#### 1A. The `undeclared_referents` receipt block (ships)

1. Every committed durable page write carries an `undeclared_referents` block in its receipt.
   It lists at most 3 names that this write newly wikilinks, that resolve to no page and no active entity in the writer's own view, and that no declaration covers.
   "Newly" uses the pre-write page state, as `entity_candidate` does.
2. Each name carries the `create-entity` route.
3. A name that the same response's `entity_candidate` block carries is omitted, so one name never gets two prompts.
   The block is the 0→1 step; `entity_candidate` stays the 1→2 step.
4. It reuses the `hints()` logic and the preflight's corpus context: no vault walk and no model call.
   It reads wikilink markup, not prose, so C6 holds.
5. The existing write-advisory fingerprints dismiss a name, and the family `undeclared_referents` can be quieted.
   It is withheld when `structural_suggestions` is `off`, exactly as `entity_candidate` is.
6. A name that matches only a page the writer may not see is listed exactly as a name with no page.

The block makes the agent's own markup into a prompt at the moment the agent holds the context.
It costs no tool-schema bytes, because the receipt is not part of the input schema.

#### 1B. `referents` declarations (gated)

The declaration argument ships only if arm C beats arm B in the measurement of decision 8.
If it does not, it stays specified here and unbuilt, and its requirements leave the spec deltas before archive.

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
   - `needs_type` or `type_unknown`: the declaration stays `pending` on the write's operation, and the outcome names the one completion call, `connect_memory(operation="complete-referents", ref=<operation ref>)`, to run after the type save.
   - `needs_create`: the caller is restricted. Until R4 ships, a restricted caller's declaration never reaches `created`; it always gets the `create-entity` route.
   - `unavailable`: a limited owner's `type` resolution needs withheld private registry definitions.
   - `unresolved`: the `ref` is not visible; a withheld ref gives the same outcome as a missing one.
   - `evidence_not_found`: see item 3.
5. Pending declarations appear in due-state (decision 5) until they complete or the agent dismisses them.
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
The reason for the raise, if the gate passes, is the measured C-over-B difference; without it the raise is not justified.
The core capture line for declarations (at most 160 bytes, served at `balanced` and `maximal` only) also ships only with the gate.

### 2. Authority: owner promotion is proactive capture (O2)

**Evidence.**
- On 2026-10-07 the owner ruled that agents promote and humans revert: a v1 owner's registry save takes effect at once (`add-vocabulary-registries` design decision 7; `activate-agent-led-vocabulary-evolution` design decision 7).
- Ruling R1 of 2026-09-28 put additive entity creation under `proactive_capture` (close-memory-loop design, "Identity batch 1 rulings").
- The served contract still requires confirmation in six places:
  - `commands.py:1313-1317`: "Decision not permission; v1 writers retain confirmation";
  - `prominence.py:202`: "Missing schema: structural_suggestions/restructure_execution; relations: link_acceptance";
  - `prominence.py:384-385` and `prominence.py:419-420`: "an affiliation relation requires link_acceptance";
  - scaffold `SKILL.md:178`: `link_acceptance` covers "accepting a suggested relation" with no owner-authored edge row;
  - scaffold `references/operations.md:38` and `references/engagement.md:63`: "affiliation relations require `link_acceptance`".
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
5. `restructure_execution` is unchanged: merge, supersession, deletion, meaning changes, alias edits on existing entries and deprecation.
   Edge removal is a removal, so `remove-relation` is `restructure_execution`; a user's revert request is its confirmation.
   Replacing a generic edge in place with a specific predicate (decision 3, item 7) is an authored typed edge, not a removal.
6. A resolved non-owner's registry save stays a pending item; its creations stay inside its write scope.
   A limited owner's save that needs withheld private registry definitions reports `unavailable`, never a success.
7. In a v2-activated vault every promotion passes the v2 effect classifier. No upgrade, migration or default activates v2, and T13 routes no v1 owner promotion to approval.
8. Task 1.1 rewrites all six served texts, and a core test fails while any of them remains.
9. This change amends now:
   - the `complete-recurring-entity-lifecycle` envelope delta, so an owner's type save follows `proactive_capture`;
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

This decision is scheduled only if the measurement of decision 8 shows an owner-anchored question failing: a fresh-session turn that names the person and needs the pet, the product or the diet.

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

**Decision.**
1. Provenance: an entity creation or typed edge writes a log entry on its page that names the originating write's path, operation id and episode key when present; a registry save keeps its history header and reason.
   `create-entity` takes the originating write's ref in its existing `ref` argument, and the `undeclared_referents` route fills it, so no new parameter is needed.
2. The due-state category `recent_promotions` holds one entry per registry addition, entity creation and typed edge between two entity pages, whichever leaf wrote it.
   A bulk writer, such as adoption or import, adds one entry per originating write, not one per page.
   A parentless type's entry carries `new_family` and is served first (R1).
   Pending declarations, if decision 1B ships, are entries with their completion call.
3. The creating session never counts its own entry.
   An entry settles at its first delivery to an interactive conversation session other than the creator's.
   A hook process, a CLI or REST call, or a session that declares itself a delegated lane never settles it. A stdio MCP session is a conversation and can settle it.
   A revert or a dismissal also settles it. A withheld promotion adds nothing to a restricted audience's count.
4. Revert routes, carried in each entry's item context:
   - A typed edge reverts by one `remove-relation` call.
   - An entity creation reverts by one curation plan. Its steps remove the promotion's own edges, then trash the entity. The preview lists every other inbound link as a dependant.
   - A registry addition reverts one key. With no dependants it is removed through a new `remove` delta verb, allowed only for a vault-added entry that nothing uses. With dependants it is deprecated (a relation to its parent), and the route lists the dependants. A revert never restores an older version and never rolls back a later save.
5. A revert runs on the user's request, which is the confirmation that deletion and removal need. History and logs keep both the promotion and its revert.

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
Then the smallest prompt ships, and each larger mechanism ships only when the measurement shows the smaller one falls short.

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
| close-memory-loop 6.2 | Independent. Other clients stay there. |
| close-memory-loop 6.3 | Contributes cross-kind topology tests if decision 4 runs. |
| close-memory-loop 6.4 | Depends on this change for the private pet-and-owner replay. |
| close-memory-loop 6.6 | Reuses the task 3.2 replay harness. |
| close-memory-loop 6.17, 6.18 | Independent; decision 4 keeps their tests and latency bound. |
| close-memory-loop 9.4 | Its `delegation-envelope` MODIFIED block must be refreshed against this change's block, whichever archives second. |
| agent-led vocabulary 2.1 | Complements. The offer needs no origins. |
| agent-led vocabulary 4.1, 4.3, 5.1, 5.5 | v2 only; off the owner's path. |
| agent-led vocabulary 7.1-7.4 | Superseded for the owner's vault by decision 2. |
| add-vocabulary-registries 5.1 (S5), 9.1 (S9) | Compatible; the offer shares S9's advisory slot. |
| capture-durable-personal-baselines 5.6 | Its affiliation sentence reads as queue acceptance under R3; task 1.1 rewrites the served copy. |
| complete-recurring-entity-lifecycle 7.5 | Its envelope delta is amended in this change. |

### 8. Proof plan (T7)

The workflow that this change alters is ordinary capture followed by later activation.
The highest-level check is the agent-track measurement of group 4, and it runs first.

**Measurement.**
1. File a new §7 amendment in `benchmarks/epistemic/PREREGISTRATION.md` before the baseline: the next free family (`f33` on this base) as its own sequence, following the f27 entry's rules.
   The founder acknowledges it before any arm result backs a comparative claim (`benchmarks/epistemic/amendments.py:95-107`).
2. Author the case set and its answer key from source truth, never from a measured session.
   Positives cover the owner's list: pet and owner, person and kin, equipment and its consumable, supplier and operator, community and membership, and a sparse existing entity.
   Each positive has a negative twin that differs by durability, not by mention count: the same name mentioned as often, but as a passing or one-off thing.
   One owner-anchored question per positive asks about the referent in a fresh session by naming the person.
3. Run every arm with the f27 driver, hookless and hooked, one run per case per client shape, on the same authored turns:
   - baseline: `main` at the base revision;
   - arm A: the authority text only (group 1);
   - arm B: arm A plus the typed-edge leaves and the receipt block (groups 2 and 3);
   - arm C: arm B plus `referents` (group 5, built on the branch).
4. Record a per-case table for each arm. A positive passes when the entity exists with a type that fits the answer key, the answer-key edge exists, and the fresh-session answer uses them. A twin passes when no entity, edge or notice exists for its name. The twin false-positive ceiling is zero.
5. The gate for decision 1B: C beats B when C passes at least one positive case that B fails, fails no case that B passes, and keeps twin false positives at zero. Otherwise `referents` does not ship.
6. The trigger for decision 4: at least one owner-anchored question fails in the shipped arm while its entity and edge exist.

**Boundary tests.** Each names the failure that only it catches.
- CI replay (task 3.2): the shipped flow through the real doors, publication and a fresh `activate_context`, with twins. It catches a break between prompt, creation, publication and activation. If decision 1B ships, the replay starts from the declaration, with no pre-saved type.
- Authority (task 1.2): a non-owner or v2 vault cannot use the new routes to bypass its gate.
- Disclosure twin (task 3.3): a withheld same-name entity at another path, and at the same path, gives a restricted caller the same receipt block, advisory and notices as a vault without it. The existing `create-entity` same-path refusal (`link.py:232-248`) stays reported debt until R4.
- Revert (task 6.2): each revert route runs as one call or one plan and leaves history.
- Surfacing (task 6.1): an entry is counted once, in the right session, and a hook or lane never settles it.

## Risks / Trade-offs

- The receipt block could become noise → at most 3 names, only newly linked unpaged names, fingerprint dismissal and family quiet.
- The measurement costs agent runs → one run per case per client shape, on the subscription.
- `referents` could be built and then dropped → it is built on the branch only after arm B is measured, and its budget raise is never taken without the gate.
- A revert can strand dependants → the route lists them before the user runs it.

## Migration Plan

1. File the amendment, author the cases and run the baseline.
2. Ship the authority text, typed-edge leaves and receipt block; run arms A and B.
3. Build `referents` on the branch, run arm C, and keep it only if it passes the gate.
4. Ship surfacing, revert and labels; run decision 4 only on its trigger.
5. Rollback: older releases ignore the new optional registry fields, the receipt block and the new due-state category; pages keep their bytes.

## Rulings (owner delegate, 2026-10-10)

- **R1. A parentless entity type** promotes at once. Its notice carries `new_family`, it is surfaced first in the next session, and it reverts in one call. Asking first would put a question in the O5 path.
- **R2. Kinship and affiliation:** this change adds a symmetric kinship relation and an affiliation relation to the core pack, as pack data. Task 2.2.
- **R3. Queue acceptance:** `link_acceptance` keeps its confirm ceiling for queue suggestions in this slice. The agent's own declared edges do not need it.
- **R4. Path occupancy for restricted writers:** suffixed filenames for restricted writers, delivered with the connector-ceiling work of `add-governed-vault-consolidation`.
- **R5. More writers:** measure the four writers first. `record_memory`, `replace_memory` and `episode_memory record` follow on evidence.

## Open items

- A delegated lane has no declared role on the wire today. Decision 5 needs one before a lane's delivery can be told apart from the owner's conversation; until it exists, only hook processes and CLI or REST calls are excluded.
- R5 named four writers; the budget in decision 1B starts `referents` on two. If the gate passes, `edit_memory` and `capture_source` need their own measurement and budget raise.
