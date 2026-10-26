# Design

## Context

The programme brief asks that ordinary work turns durable referents into entities and typed edges, and that later activation uses them.
The no-nudge architecture assigns the work: deterministic sensors measure, frozen verifiers label into queues, the active conversation agent decides, and carriers deliver state on every response.
The relation audit adds that `relates_to` is honest when nothing more specific is evidenced, that specific predicates are offered cheaply, and that relation count is never a target.

Evidence pointers below are verified against `origin/main` at `42c807797`.
Revision 2 follows an adversarial critique (REQUEST_CHANGES, 2 blocking and 7 major findings): it measures before it builds, ships the smallest prompt first, and gates the declaration argument on evidence.
Revision 3 follows the recheck (REQUEST_CHANGES, 1 blocking and 5 major findings): it preregisters the pass line that closes the change, splits the shipped arm so that the receipt block must earn its place, and makes the entity revert one call.
Revision 4 follows a third recheck (0 blocking and 2 major findings): it moves removal and revert onto tools that are already destructive, and counts twin false positives per repeat.
Revision 5 separates the revert fingerprint from the entry's review fingerprint, so a revert binds to the state the user saw while dismissal identity stays stable.

## Amendment (2026-10-26)

The owner approved the autonomous organisation plan. It amends this design as follows; the decisions below keep their history.

- **Decision 3, items 3 to 5.** `create-entity` typed edges shipped as the separate `relations=[{kind,target}]` parameter, so `connections` gains no item union. The `add-relation` operation is dropped: agents author typed edges through `relations` items and `## Relations` bullets. Edge removal survives only as an internal curation step that the promotion revert runs; no tool gains a removal operation.
- **Decision 3, item 6.** Organisation PR B replaces "endpoints never refuse a write" with a barrier: a newly added generic edge that touches an entity, where an active non-generic relation fits both endpoints, refuses before commit with at most 4 options and a one-retry route that keeps the generic relation with a recorded reason. Note-to-note generic edges are never refused; they get a warning and post-commit options. `endpoints` uses a typed grammar (`entity:<type>`, `entity:*`, `role:<note role>`) and lands with the barrier.
- **Decision 7.** The rule that the baseline exists before any part of this change merges is replaced. Organisation PR A carries no behaviour layer, so the baseline is main with PR A.
- **Decision 8.** Organisation PR B replaces the f33 protocol with the revised one: small paired development rounds, then one blind held-out run of 40 independent situations under a new preregistered amendment. The f33 amendment stays filed, sealed and unrun.

### Destructive undo binds the original incarnation

Entity undo binds the immutable page reference from the exact creation image.
Edge undo also binds both endpoint references, the canonical predicate, and the exact introducing image and transition.
Registry undo binds the registry instance, key, retained introducing version, and exact rendered overlay hash.
Paths locate subjects; a path or current fingerprint alone cannot establish continuity.

The internal proof admits every retained history file and guards its bytes and directory census through atomic commit.
It examines at most the existing compiled-history maximum-page scan budget; reaching that bound reports undo as unavailable.
Compiled page transitions must form one unique, acyclic, connected chain ending at the exact current image.
Registry transitions require a unique, acyclic suffix reachable from the introducing image; that introducing version must remain retained.
Both registry history producers record full raw before and after hashes beside the existing short audit hashes.
A missing, unreadable, inadmissible, legacy, forked, cyclic, disconnected, or capped proof reports `PROMOTION_SUBJECT_UNVERIFIED`.
The notice remains readable when admitted, but no destructive undo is offered or partially applied for unknown subjects.
A proved absence ends the original promotion; restoring the edge or key creates a separate incarnation.

The caller's revert fingerprint includes current page and history evidence for every subject.
Each sealed edge step binds the original subject descriptor and proves its continuity again before commit.
Earlier undo steps can change page bytes without invalidating another edge from the same promotion.
Fresh proof guards still reject replacement endpoints and interrupted edge continuity.

Supported moves can rewrite inbound pages without compiled history.
This change adds neither move history nor predecessor metadata, so those gaps and earlier cycles can make destructive undo unavailable.
Registry history pruning can also make undo unavailable when it removes the introducing version.
These limits affect destructive undo only; ordinary writes remain available, and public history coverage remains unverified.

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

**Budget.** `scripts/measure-tool-schema-bytes.py --json` measures the current wire objects; `tests/test_tool_schema_budget.py` holds the ceilings.

| Tool | Measured bytes | Ceiling | Change |
|---|---|---|---|
| `connect_memory` | 4,470 | 4,475 | No additive operation or connections union |
| `edit_note` | 6,906 | 6,950 | No removal operation |
| `maintain_memory` | 3,729 | 3,734 | One `revert` enum value |
| `schema_memory` | 4,185 | 4,200 | Description includes the registry `remove` verb |

The measured full surface is 102,358 bytes under a 103,089-byte ceiling.
The `revert` enum adds nine bytes; its per-tool and total raises have reasons beside their source ceilings.
Rerun the measurement after integration changes the wire objects.

### 4. Typed activation (T3, conditional)

The paired development rounds in decision 8 decide whether this conditional mechanism is needed.
Its trigger remains an owner-anchored recall miss while the entity and edge exist; the final comparison evaluates the complete candidate once.

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
- A due-state entry carries a review ref and a fingerprint (`due_state.py:478-486`).
  Review fingerprints are 24 hex characters (`review_state.py:338-370`), and `review_state.component_fingerprint` is the one shared composer of dismissal identity (`review_state.py:374-401`).
- Curation apply refuses unless the caller passes the stored plan's fingerprint (`curation.py:2829-2853`); the MODIFIED ceiling keeps that gate required.
  That fingerprint is a 64-hex SHA-256 over the plan, its binding manifest and its registry ids (`curation.py:265-266`, `curation.py:309-318`), so a review fingerprint can never match it.
- `maintain_memory` curation already takes `curation_action`, `review_ref`, `expected_plan_fingerprint` and `why` (`commands.py:10057-10100`), and curation cannot target schema or admin state (`commands.py:10110`).
- `schema_memory` saves a registry delta with `expected_hash` and `why` (`commands.py:10515-10579`).
  Its `vocabulary_ref` and `vocabulary_fingerprint` binding accepts only a vocabulary review ref with a 64-hex fingerprint and grants no write (`commands.py:9535-9551`), so it cannot bind a promotion entry.
- Active surfaces derive curation arguments from the command registry.
- `edit_memory`, `maintain_memory` and `schema_memory` are already destructive tools; `triage_memory` and `connect_memory` are not (`command_surface.py:240-264`), and generated capability text follows that registry.

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
   A dismissal settles any entry.
   An entry also settles by state, when its promotion no longer holds: the edge is absent, the entity page is in the trash, or the registry key is removed or deprecated.
   A revert therefore settles its entry without a record of its own, and so does an equivalent change made by any other route.
   A withheld promotion adds nothing to a restricted audience's count.
4. Two fingerprints do two jobs:
   - The entry's review fingerprint is the promotion's stable identity, for dismissal and settlement.
     It never folds in content hashes or dependants, so an edit to a busy page never re-raises a settled or dismissed promotion.
   - The item context serves a separate `revert_fingerprint`, computed at read time, which binds the revert to the state the user saw.
5. Every entry reverts by one call on a tool that is already destructive, and the entry's item context names that call with its `revert_fingerprint`.
   - A typed edge or an entity creation: `maintain_memory(mode="curation", curation_action="revert", review_ref=<entry ref>, expected_plan_fingerprint=<revert_fingerprint>, why=<the user's request>)`.
     The `revert_fingerprint` is `curation.plan_fingerprint` of the deterministic revert plan, computed at read time.
     Its binding manifest carries the dependant list and the content hash of each page that the plan changes.
     The call recomputes the plan from current state, seals it and applies it, and curation apply runs unchanged: a changed dependant list or content hash gives another fingerprint, so the call refuses.
     For an edge, the plan holds one `remove_relation` step. For an entity, its steps remove the promotion's own edges and then trash the entity.
   - An entity's dependants: the item context lists every other inbound link as a dependant.
     A link from the originating write is not a dependant: after the revert it is an unresolved wikilink again, as before the promotion.
     The agent passes the `revert_fingerprint` only after the user has seen and named the dependants.
   - A registry addition: one `schema_memory` save of one key with a `remove` delta, `expected_hash=<revert_fingerprint>` and `why`.
     The `revert_fingerprint` is the registry's current `expected_hash`; the save takes no `vocabulary_ref` or `vocabulary_fingerprint`.
     Curation cannot target schema state, so the registry's own destructive door carries this revert.
     The server decides at save time from current usage: with no dependants it removes the key; with dependants it deprecates the key (a relation to its parent), and the result lists the dependants.
     The `remove` verb is allowed only for a vault-added entry in a registry whose adapter declares a usage check.
     The adapters cover entity types, relations, semantic categories, note types, homes, source kinds and domains; statuses has no usage check.
     Unknown usage refuses removal instead of reporting an unused key.
     A revert never restores an older version and never rolls back a later save.
6. A revert is `restructure_execution`. It runs on the user's request, which is its confirmation; the entry's item context is its preview.
   History and logs keep both the promotion and its revert.
   An ordinary curation plan uses the same internal removal step for a wider undo.
7. No revert or removal lands on `triage_memory` or `connect_memory`, so both stay non-destructive and their marketplace text stays true.
8. Active local and Cloud surfaces must expose `curation_action="revert"` through the command registry.

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

PR A delivers the substrate first. The case author files the revised amendment before PR B is built, and PR B merges only after decision 8.
Declarations and typed activation remain conditional on misses observed in development.

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

PR A delivers the organisation substrate without a model-measurement gate.
PR B changes authoring behaviour and needs the revised paired proof before merge.
The earlier f33 amendment stays filed, sealed and unrun; the new amendment supersedes its acceptance role.
No implementer reads the held-out cases or keys.

**Independent references and filing.** A case-authoring evaluator writes invented, product-shaped source truth, cases and keys before PR B is built.
The evaluator files a new amendment under the next free family in `benchmarks/epistemic/PREREGISTRATION.md`, with case and key digests.
The owner acknowledges the amendment before the final run.
A separate measurement lane runs the arms and checks their captured inputs, outputs and digests before scoring.

**Reuse.** Use `benchmarks/epistemic/journeys/f33_referent_replay.py` for the two-session workflow, f27 client isolation, and existing tool-surface capture through a thin adapter.
Do not reuse f33's cases.
Each situation has a capture session and a fresh-recall session.
Pin the model, client, product revision, prominence and captured configuration for both arms.

**Development.** Each round asks one question on at most four paired situations, with one repeat per arm.
Change at most one thing between rounds and use fresh situations after checking a repair on the cases that exposed it.
At most five rounds use eighty actor sessions in total.
Observed misses decide whether the conditional declaration and typed-activation mechanisms are needed.
These rounds diagnose failures; they do not establish a gain.

**Final held-out comparison.** Run forty independent situations once per arm:

- Sixteen relation situations: ten with a fitting specific relation and six where generic or no edge is correct.
- Twelve referent situations: six positives and six durability twins.
- Six placement situations.
- Six vocabulary-freshness situations where a later session uses a promoted entry.

The baseline is pinned main with PR A; the candidate adds the complete PR B batch.
Twenty situations use the hooked client and twenty use the hookless client, balanced across categories.
Label the hookless arm as a proxy, not a result for another client.
The forty paired situations take 160 actor sessions.
Use deterministic source-derived scoring wherever possible.
Only residual uses-the-fact cases go to a blind judge from another model family, with the same reference view for both arms.
Use at most two judge lanes; neither receives the arm key.

**Decision.** Report the per-situation paired success difference and its 95% Newcombe paired-proportion interval.
A gain is established only when the lower bound exceeds zero.
False precision may rise by no more than one situation out of forty.
Also report omissions, duplicated reasons, abandoned writes, extra calls and re-sent bytes.
An interval containing zero means no established gain: PR B does not merge, and the per-case tables go to the owner for a ruling.
Do not expand the batch after seeing that result.

**Route and holds.** Actors use the approved Claude subscription through `claude -p`; judges use the Codex subscription.
No API or credit spend is authorized.
Development and final runs use the manager's serial laptop evaluation slot after the tool-surface evaluation.
The final run also waits for PR A's release, the common tool-surface delivery, amendment acknowledgement and allocated actor room.
A transfer of implementation custody changes none of these holds.

**Boundary proof.** Keep the real-door capture, publication and fresh-activation replay with its negative twins.
Keep authority, disclosure, one-call revert and once-per-session surfacing checks.
The case author checks that changed prompts, descriptions and guidance contain no held-out case wording.

### 9. Health cohorts

Keep the historical graph cohort unchanged and add a new authored-edge cohort split by endpoint class.
Use exact governed write deltas and server UTC timestamps from the existing atomic operation log, including its rotation archives.
The timestamp records the first server preparation of the committing attempt and survives a pending retry through its existing recovery record.
The recovery record holds only that bounded timestamp; semantic deltas are recomputed and the original auxiliary digest still guards replay.
Approval operations and structural writes without a durable timestamp binding omit optional provenance and remain a visible coverage gap.
An authored date never establishes that an uninstrumented write falls outside the server-time window.
Page dates, graph rebuild times and promotion settlement do not establish edge creation time.
Stable resolved endpoint identity prevents a spelling change or inherited predecessor edge from becoming a new authored edge.

Each metric reports its numerator, denominator, window, source and coverage; missing instrumentation is `unavailable`, never zero.
Report raw generic share, unreasoned generic edges, exact-normalized duplicate reasons, referents acted on, dismissed or ignored, and placement by actual basis.
A mutation's general rationale does not count as an edge-review reason.
Referent outcomes need correlated observations; clearing a review item does not prove that an agent acted.
Legacy periods and unsupported writers remain visible coverage gaps.

The default review target is fifteen percent generic among new authored edges that touch an entity over thirty UTC days.
The vault may configure the target and window; crossing it raises only an audit or upkeep signal.
Note-to-note edges have no target, and no metric refuses a write.

## Risks / Trade-offs

- The receipt block could become noise → at most 3 names, only newly linked unpaged names, fingerprint dismissal and family quiet, and the keep rule drops it unless it earns its place.
- Measurement uses at most eighty development sessions and 160 final sessions, plus residual blind judging.
  The manager allocates the serial laptop slot and subscription room; no runtime estimate establishes readiness.
- `referents` could be built and then dropped → it is built only when the shipped arm misses the pass line, and its budget raise is never taken without the gate.
- A revert can strand dependants → the item context lists them, and the `revert_fingerprint` that the revert requires covers them.
- Bootstrap bytes are nearly spent → task 1.1 pays for `promotion_classes` by trimming the confirm-first texts it replaces, on both the compact core ceiling and the compact reference bound, and raises neither (decision 2, item 9).

## Migration Plan

1. Deliver PR A through independent review, green CI and the ordinary release; the substrate becomes the comparison baseline.
2. File the revised amendment and independent source-derived cases before building PR B; preserve the held-out set.
3. Run the permitted development rounds, then the acknowledged final comparison; record the decision and release only an accepted batch.
4. Deliver the remaining note-type, root and Studio work in the approved order; keep the sensor follow-on gated on its readings ledger.
5. Before rollback, move homed pages to their type folders if the previous writer must edit them.
6. Reinstall the previous release; retained history and overlay tolerance preserve pages and registry additions.

The previous writer can move a homed page to its type folder, but it refuses edits while the page remains in the home.

## Rulings (owner delegate, 2026-10-10)

- **R1. A parentless entity type** promotes at once. Its notice carries `new_family`, it is surfaced first in the next session, and it reverts in one call. Asking first would put a question in the O5 path.
- **R2. Kinship and affiliation:** this change adds a symmetric kinship relation and an affiliation relation to the core pack, as pack data. Task 2.2.
- **R3. Queue acceptance:** `link_acceptance` keeps its confirm ceiling for queue suggestions in this slice. The agent's own declared edges do not need it.
- **R4. Path occupancy for restricted writers:** suffixed filenames for restricted writers, delivered with the connector-ceiling work of `add-governed-vault-consolidation`.
- **R5. More writers:** measure the four writers first. `record_memory`, `replace_memory` and `episode_memory record` follow on evidence.
- **R6. Pass line:** the 2026-10-26 approved revision replaces the former f33 pass line for this delivery with decision 8. The filed f33 amendment remains sealed.

## Open items

- A delegated lane has no declared role on the wire today. Decision 5 lets the server exclude a lane once a field identifies one; until then, only hook processes and CLI or REST calls are excluded from settling an entry.
- R5 named four writers; the budget in decision 1B starts `referents` on two. If the gate passes, `edit_memory` and `capture_source` need their own measurement and budget raise.
