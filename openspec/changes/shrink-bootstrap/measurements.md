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

## 5. Everything else Exomem injects (scope addition)

Reproduce: `python scripts/context-footprint.py`. Exact where a constant exists; modelled where cadence depends on the agent. Model assumptions: 50 turns, one prompt and one Stop each, 144 s apart (2 hours), 10% of prompts are short control prompts, 70% of balanced turns (90% at maximal) end in text long enough to count as substantive, the agent writes and records nothing (worst case for the nudges; recording resets only the episode counter, and the cooldowns bind first, so complying changes the modelled counts by nothing at these cadences).

### 5.1 What fires, when, and what it costs

| injection | source | trigger | bytes each | notes |
|---|---|---|---:|---|
| Skill or bootstrap carrier | `SKILL.md` (30,175) loaded when the skill fires; `bootstrap` result | once per session, agent-initiated | 22,008 (session profile) to 63,055 (compact) | no hook injects at a fresh start: `SessionStart` is registered only for `compact\|resume` |
| Continuation checkpoint | `exomem_continuation_checkpoint.py` `render_continuation` | `SessionStart` on compact/resume, `PreCompact`/`SessionEnd` write only | up to 4,096 (`MAX_CONTEXT_BYTES`) | includes a checkpoint id, a transcript-binding line with a sha256 (meaningful to the hook, not the model) and a 400 B advisory |
| Retrieval check | `exomem_retrieve_nudge.py` `REMINDER` | every `UserPromptSubmit` past the length, control-prompt and cooldown gates; balanced: session cooldown 300 s and client-wide 900 s; maximal: no cooldown | 861 | repeats verbatim; its own text says "do not repeat a KB search just because this reminder appears again" |
| Working-set block | same hook, `EXOMEM_RETRIEVE_INJECT=working_set` | **opt-in, off by default**; replaces the reminder on gated prompts; referential prompts always | header 278 + up to 4,000 | ceiling by design; a real fill was not measurable without a live vault |
| Capture check | `exomem_capture_nudge.py` `REMINDER` | `Stop`, when the turn's text is long enough and no write or `Saved ->` marker landed; balanced: min 300 chars, cooldown 300 s; maximal: 120 chars, 60 s | **1,841** | blocks the stop (`decision: block`), so each fire also buys an extra model turn |
| Episode check | same hook, `EPISODE_ASK` | `Stop`, every 6 substantive turns (balanced, 1,200 s cooldown) or 3 (maximal, 600 s); takes precedence over the capture check on that Stop | 495 | blocks the stop |
| Episode coverage | same hook, `COVERAGE_ASK` | only when the session prepared an episode candidate | ~460 | not modelled |
| MCP `episode_due` | `activate_context` packet | tool-only clients, after 8 activations, 1,800 s cooldown | one sentence | already bounded |
| MCP tool schemas | 32 tools, `tests/fixtures/mcp_tool_schemas.json` | resent every turn by clients that do not defer tools; occupies the window either way | **169,911** total: descriptions 37,137, input schemas 131,071 (per-parameter descriptions 60,507) | largest single cost, see 5.3 |
| Skill front matter | 10 plugin skills | listed in the system prompt every turn | 3,041 | |

Nudge counts in the 50-turn model: balanced 15 capture + 5 episode Stops (20 of 50 Stops, 30,090 B) and 7 retrieval reminders (6,027 B); maximal 35 + 10 (45 of 50 Stops, 69,385 B) and 45 retrieval reminders (38,745 B). Each of those Stop blocks is also one extra model turn that re-reads the whole context.

### 5.2 Where the capture check's 1,841 bytes come from

`REMINDER` restates, in telegraphic form, most of what `engagement.contract.capture` already serves in the bootstrap core at balanced and maximal: reuse evidence, decompose before routing, hydrate and resolve entities, supersede rather than correct beside, intent to Planning and observed outcome to Records, "selected is not write consent", no local scan, no transcripts. The hook fires it dozens of times per session against a bootstrap the agent read once.

### 5.3 The tool schemas are the largest cost, and the diet so far does not touch them

32 tools, 169,911 B (about 42,500 tokens): larger than the whole bootstrap plus every hook nudge in a 50-turn session combined. Prompt caching lowers what a client pays per turn but not the window it occupies. Heaviest: `edit_memory` 13,885 (description 3,961), `manage_memory_file` 13,627, `remember` 12,712 (description 3,494), `replace_memory` 11,238, `activate_context` 9,607 (description 5,479), `connect_memory` 9,280 (50 parameters), `episode_memory` 7,776, `record_memory` 7,619, `maintain_memory` 7,576, `schema_memory` 7,483, `observe_memory` 7,338. Of the 131,071 B of input schemas, 60,507 B are per-parameter descriptions and about 70 KB is JSON-schema structure (types, enums, defaults). The pinned baseline is `tests/fixtures/mcp_tool_schemas.json`; hosted v1 to v4 pin their own legacy schemas and are unaffected by live-registry edits.

### 5.4 Fifty-turn session, before and after (bytes injected, once-per-session plus per-turn)

Balanced level, hooks installed, default (reminder-only) retrieve mode, one compaction in the session. "Claude Code" carries the skill and the session bootstrap; "Codex / generic" carries the compact bootstrap. After (A) shrinks the text and keeps every cadence; After (B) also moves to at most one Stop nudge per episode window and one retrieval reminder per session (re-armed on compaction). Design in `design.md` decisions 6 to 9.

| item | Claude Code before | after A | after B | Codex / generic before | after A | after B |
|---|---:|---:|---:|---:|---:|---:|
| session carrier (skill + bootstrap, or compact) | 52,183 | 30,300 | 30,300 | 62,698 | 13,600 | 13,600 |
| continuation checkpoint (1 compaction) | 4,096 | 1,900 | 1,900 | 4,096 | 1,900 | 1,900 |
| Stop nudges (capture + episode) | 30,090 | 6,150 | 1,900 | 30,090 | 6,150 | 1,900 |
| retrieval reminders | 6,027 | 1,800 | 500 | 6,027 | 1,800 | 500 |
| **injected total** | **92,396** | **40,150** | **34,600** | **102,911** | **23,450** | **17,900** |
| tokens (bytes / 4) | ~23,100 | ~10,000 | ~8,650 | ~25,700 | ~5,900 | ~4,500 |

Same, at maximal (recall before every turn, 45 Stops nudged before):

| item | Claude Code before | after A | after B | Codex / generic before | after A | after B |
|---|---:|---:|---:|---:|---:|---:|
| session carrier | 52,540 | 30,600 | 30,600 | 63,055 | 13,900 | 13,900 |
| continuation checkpoint | 4,096 | 1,900 | 1,900 | 4,096 | 1,900 | 1,900 |
| Stop nudges | 69,385 | 13,800 | 3,800 | 69,385 | 13,800 | 3,800 |
| retrieval reminders | 38,745 | 11,700 | 4,100 | 38,745 | 11,700 | 4,100 |
| **injected total** | **164,766** | **58,000** | **40,400** | **175,281** | **41,300** | **23,700** |

Resent per turn, not in the totals above (cached by most clients, but resident in the window): tool schemas 169,911 (about 110,000 after design decision 10, -35%) and skill front matter 3,041 (about 1,600 after). Over 50 turns that is 8.5 MB resent before and 5.5 MB after; the dieted bootstrap and nudges are about 1% and 0.5% of that. The window occupancy that a cold session pays is what matters to attention, so the ordering by size is: tool schemas (170 KB) > bootstrap or skill (22 to 63 KB) > Stop nudges over a session (30 to 69 KB) > retrieval reminders > checkpoint.

The "after" figures are design estimates from the target sizes in `design.md`; Phase 2 replaces them with measurements.

## 6. As built (Phase 2 measurements, same method and tree as sections 1 to 5)

Reproduce: `scripts/bootstrap-byte-breakdown.py --sections`, `scripts/context-footprint.py`. Hosted v1 to v4 are unchanged byte for byte (digest-pinned by `tests/test_bootstrap_frozen_profiles.py`); their "after" is their "before".

### 6.1 Compact bootstrap, bytes (before to after core), by surface and level

| surface | off | light | balanced | maximal |
|---|---:|---:|---:|---:|
| default (generic MCP) | 60,481 to 11,747 | 60,765 to 12,031 | 62,698 to 13,973 | 63,055 to 14,330 |
| claude-code | 60,490 to 11,756 | 60,774 to 12,040 | 62,707 to 13,982 | 63,064 to 14,339 |
| hosted-alpha-agent-v5 | 59,025 to 11,570 | 59,309 to 11,854 | 60,960 to 13,514 | 61,203 to 13,757 |
| hosted-alpha-agent-v1 (frozen) | 51,906 | 52,190 | 53,841 | 54,084 |
| hosted-alpha-agent-v3 (frozen) | 56,448 | 56,732 | 58,383 | 58,626 |
| hosted-alpha-agent-v4 (frozen) | 58,760 | 59,044 | 60,695 | 60,938 |

Worst case (claude-code, maximal) 14,339 against the ruled 15,000 ceiling: 661 bytes of margin, above the 512-byte warning band. `bootstrap(section="all")` returns the complete pre-core payload (63,055 at default/maximal). Section sizes at maximal: authoring 18,992; routing 11,375; entities 7,454; adoption 6,510; records_planning 5,360; epistemics 3,138; envelope 1,917; diagnostics_reading 1,128.

`profile="session"` (default surface): 18,726 / 19,010 / 20,943 / 21,300 at off / light / balanced / maximal, against 22,008 at maximal before. It is rebased on the core (core `engagement` and `server`, plus the section index) but does not get much smaller: the tests pin the live state a skill-holding client is served (workflow contracts, relation vocabulary, entity registry, source taxonomy, vocabulary workflow, the post-write handling the skill does not carry), and that state is most of its size.

### 6.2 Fifty-turn coding session, bytes injected

Model as in section 5. "Before" uses the base constants (capture 1,841, episode 495, retrieval 861); "after" uses what shipped: the full capture text once and again after one compaction then a 341-byte short line, the episode ask at 384, the retrieval reminder at 358 once (and once more after a compaction) and, at maximal, a 195-byte pointer per prompt. Stop cadence B was not shipped (below), so Stop fire counts are unchanged.

Balanced:

| item | Claude Code before | after | Codex or generic before | after |
|---|---:|---:|---:|---:|
| session carrier (skill + session, or compact) | 51,826 | 37,050 | 62,698 | 13,973 |
| continuation checkpoint (1 compaction, cap) | 4,096 | 2,048 | 4,096 | 2,048 |
| Stop nudges | 30,090 | 10,035 | 30,090 | 10,035 |
| retrieval reminders | 6,027 | 716 | 6,027 | 716 |
| **total** | **92,039** | **49,849** | **102,911** | **26,772** |

Maximal:

| item | Claude Code before | after | Codex or generic before | after |
|---|---:|---:|---:|---:|
| session carrier | 52,183 | 37,407 | 63,055 | 14,330 |
| continuation checkpoint (cap) | 4,096 | 2,048 | 4,096 | 2,048 |
| Stop nudges | 69,385 | 18,775 | 69,385 | 18,775 |
| retrieval reminders | 38,745 | 9,101 | 38,745 | 9,101 |
| **total** | **164,409** | **67,331** | **175,281** | **44,254** |

The Claude Code carrier stays large because the skill (16,107 bytes now, from 30,175) and the session profile are both loaded. Tool schemas (170 KB) are untouched here: that lane was split out by ruling, and the only schema change in this change is the new `section` parameter on `bootstrap` (+229 bytes).

### 6.3 What was not done, and why

- **Stop cadence B (one nudge per episode window).** Gated on the no-nudge benchmark showing initiation parity. That needs a live-agent run (`close-memory-loop` task 6.1, open), which is not available here, so parity is unproven and, per the ruling, option A shipped.
- **Working-set silent-when-unchanged.** Opt-in and off by default, not part of the ruling; not implemented.
- **Checkpoint id and transcript-binding lines** stay in the model-facing text: existing tests pin them.
- **"Withheld is absent" sentence.** Dropped by ruling: server-enforced.
- **Pre-existing finding, not from this change:** on hosted v4 and v5 the surface filter drops the whole `recall` contract (it opens with the `activate_context` carrier, and hosted does not export `activate_context`), so hosted clients receive no recall-before-answering text in the bootstrap. The core manifest test skips that rule on hosted surfaces and says why. v1 to v4 are frozen; v5 could be fixed by making the recall line surface-aware. Needs a ruling.
