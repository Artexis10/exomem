# Measurements (2026-09-29, integration/wave-bcd @ f3bfe7be, exomem 0.96.0)

Reproduce: `uv run python scripts/bootstrap-byte-breakdown.py --sections`. Measure is `len(json.dumps(payload))` on an empty vault, the same measure `tests/test_bootstrap_compact_budget.py` uses. Vault-derived blocks (`due_state`, entity types beyond the built-ins, extension vocabulary, workflow-contract inventory) are empty here, so a real vault only adds to these numbers (each such block is bounded). Tokens are bytes / 4.

## 1. Totals, compact profile

| surface | off | light | balanced | maximal |
|---|---:|---:|---:|---:|
| default (generic MCP) | 60,481 | 60,765 | 62,698 | 63,055 |
| claude-code | 60,490 | 60,774 | 62,707 | 63,064 |
| hosted-alpha-agent-v1 | 51,906 | 52,190 | 53,841 | 54,084 |
| hosted-alpha-agent-v3 | 56,448 | 56,732 | 58,383 | 58,626 |
| hosted-alpha-agent-v4 | 58,760 | 59,044 | 60,695 | 60,938 |
| hosted-alpha-agent-v5 | 59,025 | 59,309 | 60,960 | 61,203 |

Other profiles on the default surface at `maximal`: `full` 117,541; `diagnostics` 117,571; `session` (a client holding the skill contract) 22,008.

What the matrix says:

- **The level barely moves the total.** Only `engagement` differs between levels (2,904 at `off`, 3,188 `light`, 5,130 `balanced`, 5,487 `maximal`); everything else is identical. `off` pays 96% of `maximal`.
- **The surface barely matters either**, except where a profile withholds commands. `claude-code` is +9 bytes over default. Hosted v5 is 1.85 KB smaller only because `transfer_artifact` and a few routes are withheld there; v1 is 9 KB smaller because `records` (2,525 to 101), `product_commands` and `tool_defaults` are cut to the v1 command list. There is no "ChatGPT" or "claude.ai" bootstrap of its own: those clients reach a hosted profile (v1 to v5 by cell) and, being hookless, default to `maximal` (`prominence.HOOKLESS_SURFACES`), so they take the largest level of a hosted profile.
- **Nobody is served less than 51.9 KB.** The smallest compact payload in the matrix is hosted v1 at `off`.
- **Worst case for the ceiling** is claude-code at `maximal`, 63,064 bytes, 236 under `COMPACT_BYTE_CEILING`.

## 2. By section, default surface

| section | off | balanced | maximal | share at maximal | used for | needed |
|---|---:|---:|---:|---:|---|---|
| authoring_contract | 9,902 | 9,902 | 9,902 | 15.7% | how to write a compiled note: canonical loop, preflight, reviewed creation, post-write advisories, note-type recipes | canonical loop and post-write restraint every session that writes; recipes, reviewed-creation and per-advisory handling only when that case arises |
| semantic_authoring | 9,042 | 9,042 | 9,042 | 14.3% | the observation syntax, minimum semantic unit, portable categories, findings and roles | only when authoring a semantic unit; a one-line rule covers the always-case |
| engagement | 2,904 | 5,130 | 5,487 | 8.7% | recall, capture, narration and hook-cadence contract for the level; delegation envelope | **every session** (recall/capture text); envelope class table only when a restructure is proposed |
| vocabulary_workflow | 4,566 | 4,566 | 4,566 | 7.2% | how to review and decide entity/relation vocabulary at a durable capture boundary | on demand: at a capture boundary that raises a vocabulary question |
| knowledge_packs | 3,935 | 3,935 | 3,935 | 6.2% | catalogue of six built-in packs and the selected pack's guidance | on demand: adoption, first run, pack selection |
| simple_actions | 3,885 | 3,885 | 3,885 | 6.2% | intent to tool routing per front-door action | every session, but duplicated (see below) |
| epistemic_contract | 3,114 | 3,114 | 3,114 | 4.9% | append-only raw material, supersede never overwrite, capture-the-outcome, vocabulary | **every session** (commitments); vocabulary and recipes on demand |
| records | 2,525 | 2,525 | 2,525 | 4.0% | Records routing, intent vs outcome boundary, examples | on demand: user reports an observed outcome or asks about Records |
| front_door_actions | 2,498 | 2,498 | 2,498 | 4.0% | second statement of the same routing, with selected-pack guidance | duplicate of simple_actions |
| workflow_skills | 2,196 | 2,196 | 2,196 | 3.5% | index of shipped workflow skills (name, purpose, triggers, path) | on demand: a client with the skill installed already has the index; one without has no use for the paths |
| workflow | 2,062 | 2,062 | 2,062 | 3.3% | the working loop, save rule, miss rule | **every session** |
| entity_registry | 2,021 | 2,012 | 2,012 | 3.2% | entity types, capture rule, lifecycle and hydration continuation | capture rule every session; lifecycle detail on demand |
| product_commands | 1,802 | 1,802 | 1,802 | 2.9% | third statement of the tool list, plus routes | duplicate; the tool list is in `tools/list` |
| planning | 1,671 | 1,671 | 1,671 | 2.7% | Planning inventory and boundaries | on demand |
| search_guidance | 1,557 | 1,557 | 1,557 | 2.5% | find knobs, score interpretation, relation filters | every session for the two-line rule; score and filter detail on demand |
| tool_defaults | 1,274 | 1,274 | 1,274 | 2.0% | fourth statement of routing defaults (lookup, upload, supersede) | duplicate |
| workflow_contracts | 1,113 | 1,113 | 1,113 | 1.8% | workflow-contract resolution route and invariants | on demand |
| server | 854 | 854 | 854 | 1.4% | name, version, compute policy, tool-surface digests | version and digest every session; compute policy on demand |
| active_capabilities | 827 | 827 | 827 | 1.3% | which tools this surface can actually call, with a digest | **every session** (truth about the surface) |
| governance | 593 | 593 | 593 | 0.9% | disclosure model, purpose declaration, data-not-command rule | **every session** |
| relation_vocabulary | 465 | 465 | 465 | 0.7% | core relation set and the resolve route | on demand |
| memory_model | 319 | 319 | 319 | 0.5% | built-in AI memory vs Exomem | on demand (first run) |
| source_taxonomy | 319 | 319 | 319 | 0.5% | source_kind/domain vocabulary is open | on demand: when classifying a source |
| performance_profiles | 236 | 236 | 236 | 0.4% | how to read diagnostic timings | on demand |
| common_tools, common_actions | 215 | 215 | 215 | 0.3% | fifth and sixth statements of the tool list | duplicate |
| contract_version, profile | 23 | 23 | 23 | 0.0% | identity | every session |
| **total** | **60,481** | **62,698** | **63,055** | | | |

Blocks needed on every session: engagement contract 3.1 KB, workflow 2.1 KB, governance 0.6 KB, active_capabilities 0.8 KB, epistemic commitments 1.1 KB, the write essentials of authoring_contract (canonical_loop 1.0 KB, preflight 0.4 KB, write_feedback and due-state restraint about 1.2 KB), the intent routing (one statement, not five), search rules, entity capture rule. That is about 12 KB of the 63 KB. The remaining ~51 KB is reference material used when a specific task arises.

## 3. By field, the six largest blocks (maximal)

- **authoring_contract 9,902**: post_write 5,618 (due_state_handling 502, due_state 485, capture_sweep_handling 451, structure_suggestion_handling 444, artifact_role_state_handling 435, family_disposition 374, structure_suggestion 336, family_disposition_reading 333, destination_choice 292, collection_candidate 277, write_feedback 236, review_reason 203, records_routing 189, due_state_authority 173, records_routing_handling 140, remember_suggestions 104, structure_suggestion_authority 91, accepted_links 88); note_type_recipes 1,023 (nine recipes); canonical_loop 1,005; reviewed_creation 766; route_by_intent 517; preflight 404; semantic_units 213; reviewed_existing_edit 190. Post-write advisory text is about 57% of the block and describes fields that arrive only after a write.
- **semantic_authoring 9,042**: compact 2,531 (category 503, tags 397, anchor 224, content 219, exclusions 174, context 172, ...); portable_categories 2,175; minimum_semantic_unit 1,881; rich 896; findings 522; semantic_roles 399; routes 366.
- **vocabulary_workflow 4,566**: decision 1,668; application 412; relation_type 378; cadence 374; relation_question 251; entity_type 251; entity_instance 217; consideration 194; question 183; authority 122; context 115; review_route 107; families 60.
- **knowledge_packs 3,935**: selected 1,870 (packs 1,661); available 1,864; selection_rule 152. The catalogue of all packs is served although only the selected pack applies.
- **simple_actions 3,885**: maintain 585, capture 543, review 507, ask 501, remember 405, connect 366, record 301, adopt 291, plan 278.
- **epistemic_contract 3,114**: commitments 1,138; vocabulary 1,072; capture_the_outcome 568; capture_nudge 259.
- **engagement 5,487**: contract 3,092 (capture 2,045, recall 384, effective_capture 338, narration 151, summary 78); envelope 1,887 (protocol 609, classes 598, confirm_required 335, founder_gate 258); hook_cadence 271.
- **records 2,525**: intent_boundary 749, capture_examples 432, software_rule 181, review_rule 146.

Skill scaffold, for the retrieval path (bytes on disk, not served): SKILL.md 30,175; references total 210,960 (operations.md 50,886; writing.md 25,033; page-types.md 19,855; operation-routing.md 18,306; engagement.md 17,137).

## 4. Rules the core must keep, and where they live today

| rule | today | note |
|---|---|---|
| Recall before answering | `engagement.contract.recall` (384), `summary` | carried at `balanced` and `maximal`; deliberately absent at `light`/`off` |
| Capture loop | `engagement.contract.capture` (2,045), `effective_capture`, `authoring_contract.canonical_loop` | |
| Episode recording | tail of `engagement.contract.capture` (episode-completeness pass), `episode_memory` in routing | |
| Withheld = absent | **not served as a sentence.** Enforced by the server (governance kernel, egress); the payload carries only `governance.disclosure_model` (data-not-command) and `workflow.miss_rule` ("empty means not found in that scope") | see Needs ruling |
| Supersede, never overwrite; raw material append-only | `epistemic_contract.commitments` | |
| Delegation ceiling | `engagement.envelope` | |
| Due-state restraint | `authoring_contract.post_write.due_state_*` | |
