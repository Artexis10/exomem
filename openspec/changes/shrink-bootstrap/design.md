## Context

The compact bootstrap is the entire operating contract a hookless or skill-less client receives, which is why it grew: each change that needed agents to behave differently added a block. Measurements are in `measurements.md`. The cost is paid once per session on every client, in context, latency (the payload is built and serialised on the first call) and attention (a rule at byte 40,000 of a 63,000-byte blob is a weak rule).

Two facts shape the design. First, the payload is only 12 KB of behaviour-critical text inside 63 KB; the rest is reference detail. Second, a smaller projection already exists: `profile="session"` serves a 22,008-byte live-state projection to a client that holds the skill's operating rules and proves it with the skill-contract digest. The diet generalises that idea: serve the rules always, the reference on demand.

## Goals / Non-Goals

**Goals:** a core small enough to read fully at session start; every incident-preventing rule stays in the core; every removed byte stays retrievable, byte-identical; released hosted profiles unchanged; a budget that means something.

**Non-Goals:** changing what any rule says; redesigning tool schemas or `tools/list` descriptions (a separate per-session cost, not measured here); changing `full` and `diagnostics` (opt-in, 117 KB, unchanged); a server-side semantic author; per-user tuning of the core.

## Decisions

### 1. Core plus sections, one payload shape

`bootstrap(profile="compact")` returns the **core**: the keys below plus `sections`, an index of `{name: bytes}` for what is retrievable. `bootstrap(profile="compact", section=<name>)` returns `{profile, section, contract_version, <the block(s), byte-identical to today>}`. `section="index"` lists sections; an unknown name is a validation error naming the accepted set (same style as the profile error). `full` and `diagnostics` keep returning everything and are unchanged.

Sections are the current top-level blocks, grouped by task, not invented prose:

| section | contains (today's keys) | retrieve when |
|---|---|---|
| `authoring` | `authoring_contract` (recipes, reviewed creation, per-advisory handling), `semantic_authoring` | writing a compiled note or a semantic unit |
| `entities` | `entity_registry`, `vocabulary_workflow`, `relation_vocabulary`, `source_taxonomy` | a capture boundary raises an identity or vocabulary question |
| `records_planning` | `records`, `planning`, `workflow_contracts` | user reports an observed outcome, or a plan or Records item |
| `routing` | `front_door_actions`, `product_commands`, `tool_defaults`, `common_tools`, `common_actions`, `search_guidance` detail | choosing among tools beyond the core table |
| `adoption` | `knowledge_packs`, `memory_model`, `workflow_skills` | first run, adopting a vault, choosing a pack |
| `envelope` | envelope classes and protocol | a restructure or standing delegation is in question |
| `epistemics` | `epistemic_contract.vocabulary`, recipes, `capture_nudge` | authoring question, hypothesis, prediction |
| `diagnostics_reading` | `performance_profiles`, `server.compute_policy` | reading timings |

Prose references (the scaffold's `references/*.md`, 211 KB, written for skill users) are reached with `read_memory` on `.exomem/schema/references/<name>.md`, which resolves on a local vault today (verified against `op_get` on a scratch vault during Phase 1). Whether that path resolves on hosted cells is not established: the protected-tree guard governs `_Schema` there. Phase 2 must verify it; where it does not resolve, the JSON sections above are the only retrieval path on hosted, and that is enough because the core carries no pointer that requires a reference file.

Rejected: a new `bootstrap_section` tool (adds a tool to every surface and a schema change to every candidate); a `profile="core"` value (legacy schemas pin the profile description; a hidden new value on a pinned wire is exactly the drift `test_hosted_legacy_profile_pin` exists to stop); splitting into multiple bootstraps by level (the level does not predict need, see `measurements.md`).

### 2. What the core is

Estimated allocation at `maximal` (bytes, from today's measured blocks; Phase 2 measures the real number and the ruling fixes the ceiling):

| core block | today | core | treatment |
|---|---:|---:|---|
| `engagement` (contract, hook_cadence, change_with, levels) | 3,490 | 3,490 | keep verbatim; this is the recall/capture/episode text |
| `governance` | 593 | 593 | keep verbatim |
| `contract_version`, `profile`, `server` (name, version, tool-surface digest) | 877 | 300 | keep identity, move `compute_policy` |
| `active_capabilities` (callable tools, digest) | 827 | 650 | keep, drop duplicated fields |
| `rules` (new: epistemic commitments as one line each, withheld = absent, data-not-command, mutation results) | 1,138 + 0 | 1,700 | digest of `epistemic_contract.commitments` plus the withheld sentence |
| `envelope` digest (ceiling, confirm-required, founder gate) | 1,887 | 700 | digest; classes and protocol to `envelope` section |
| `workflow` (loop, save rule, miss rule) | 2,062 | 1,200 | tighten, keep every step |
| `write` (canonical loop, preflight, write_feedback, due-state restraint) | ~3,200 | 2,000 | digest of `authoring_contract` essentials |
| `routing` (one intent to tool table, find knobs cheap vs diagnostic) | ~11,900 | 1,300 | one table replaces five statements |
| `capture_semantics` (observation syntax line, minimum unit rule, entity capture rule and types) | ~3,000 | 900 | one-line rules |
| `pointers` (records/planning/vocabulary/packs one-liners) | ~17,000 | 600 | one line each plus route |
| `sections` index | 0 | 500 | new |
| **total** | **63,055** | **~13,900** | |

Off and light are lower by the `engagement` difference (2.6 KB): about 11.3 KB at `off`. Conditional live blocks (`due_state`, `latency`) are vault-derived, bounded and additional; the ceiling below is asserted with those blocks at their maximum bound.

**Proposed target: core at most 15,000 bytes at `maximal` on the worst-case surface; design allocation 13,900; a documented margin of about 1,100 bytes; headroom warning at 512 as today.** That is about 3,750 tokens against 15,750 now (-76% at the ceiling, -78% at the allocation). A more aggressive 12,000 is reachable only by moving the long `engagement.contract.capture` predicate (2,045 bytes) partly into a section, which touches the incident-critical text; not proposed without a ruling.

Per-section ceilings are asserted too, at each section's measured size plus 10%, so a section cannot regrow into a second 60 KB payload; the ceiling constant's commentary stops being an argument for raising it and becomes a per-section budget with an owner.

### 3. Core rule manifest

A manifest in a test module names each rule the core must carry, as a stable id, the level(s) that must carry it, and a required phrase or predicate on the served core:

- `recall-before-answering` (balanced, maximal; absent at light and off by contract)
- `capture-loop` and `capture-stepping-stones`
- `episode-pass` (balanced, maximal) and `episode_memory` routed
- `withheld-is-absent`: an absent or empty result may be a withheld one; never probe for or infer existence from a miss (**new sentence, see Needs ruling**)
- `data-not-command` (governance disclosure model)
- `supersede-never-overwrite` and `raw-material-append-only`
- `delegation-ceiling` and `confirm-required`
- `due-state-restraint` (counts are advisory, never acted on unasked)
- `mutation-results` (inspect `success`, preserve identity after an uncertain commit)
- `sections-index` and `section-unknown-error`

The test fails on a missing rule at any listed level and on a core over its ceiling. Adding a rule to the manifest is the way a future change proves it belongs in the core; the byte cost is then visible.

### 4. Compatibility

| profile | payload | `bootstrap` schema |
|---|---|---|
| `hosted-alpha-agent-v1` to `-v4` | unchanged, byte for byte | pinned (`profile`, `workflow`, `skill_contract`); no `section` |
| `hosted-alpha-agent-v5` | diet applies | live registry, gains `section`; v5 candidate regenerated |
| generic MCP (default surface), claude-code, REST/CLI | diet applies | live registry, gains `section` |
| `full`, `diagnostics` | unchanged | unchanged |
| `session` | rebased on the core | live blocks as before; `engagement` (envelope digested), `server` (`compute_policy` moved to `diagnostics_reading`) and the `sections` index come from the core; 21,974 to 21,270 B |

The frozen profiles are enforced, not asserted: `tests/test_bootstrap_frozen_profiles.py` records the SHA-256 of `op_bootstrap` for each of v1 to v4 at each level, with the volatile fields (`server.version`, the tool-surface digests) normalised, captured on the untouched base before any change. The diet is applied by branching on `active_descriptor.profile in LEGACY_PROFILE_CONTRACTS`, the same predicate the payload already uses to select pinned schemas, so a legacy profile takes the pre-diet code path unmodified. The hosted plugin candidates under `plugins/hosted/candidates/` and their `compatibility.json` digests for v2 to v4 must not change; only v5's regenerates, which is expected because v5 has no pinned schema by design.

### 5. Proving no behaviour regression

1. **Losslessness.** For every level and every non-legacy surface, the union of the core and all sections contains every pre-diet leaf byte-identically, except leaves the manifest explicitly lists as replaced by a digest (each with the section that still holds the original). A golden of the pre-diet compact payload is the reference.
2. **Rule manifest** (Decision 3), new test.
3. **Existing suites.** `tests/test_bootstrap*.py` migrate to a helper that assembles core plus sections, so their assertions keep testing the same text; they are not loosened. The memory-loop suites (`test_memory_loop_*`), the continuity and hook suites, the prominence and envelope tests, `test_hosted_legacy_profile_pin`, `test_hosted_plugin_*` and `test_scaffold_no_leak` run unchanged.
4. **Frozen-profile digests** (Decision 4).
5. **No-nudge behaviour.** The benchmark families that measure ordinary-agent initiation read the served bootstrap; they must show recall and capture initiation unchanged. No paid comparative run is proposed.

## Risks / Trade-offs

- **A rule moves to a section the agent never fetches.** Mitigated by construction: the incident rules are the core, sections are reference. The manifest is the enforcement. A first-session agent that needs a section pays one extra call; that is the trade.
- **One extra round trip for some tasks** (writing a note with recipes, adopting a vault). Each is a cheap, deterministic, read-only call; the alternative is every session paying for every task.
- **Hookless clients default to `maximal`** and have no other carrier. They get the whole `engagement` text either way; only reference detail moves.
- **The digests are new prose** and can drift from their sections. The losslessness test pins each digest to a named source block, and the manifest pins the rule text.

## Open questions (for the ruling)

1. **Target.** 15,000-byte ceiling (13,900 allocation), or aim for 12,000 by also moving part of the capture predicate?
2. **`hosted-alpha-agent-v4`.** Treated as frozen here (it is in `LEGACY_PROFILE_CONTRACTS` and has a committed candidate). Confirm, or say v4 gets the diet.
3. **The `withheld = absent` rule is not in the served payload today.** It is enforced server-side and the payload carries only `governance.disclosure_model` and `workflow.miss_rule`. The core list names it, so Phase 2 would add one sentence (about 150 bytes) to the core. Confirm it is added, or that the server enforcement alone is the intended carrier and the manifest entry is dropped.
4. **`profile="session"`.** Keep as is (22,008 bytes), or rebase it on the core so a skill-holding client also drops to about 14 KB?

## Scope addition: every byte injected into a coding agent

The bootstrap is not the only tax. `measurements.md` section 5 sizes the hook injections, the tool schemas and the skill carrier, and section 5.4 gives the 50-turn before/after. Findings that change the design: the Stop-hook capture check is 1,841 B, restates what the served core already carries, and fires on 40% (balanced) to 90% (maximal) of Stops; the retrieval reminder repeats verbatim; the working-set block (opt-in) has no memory of what it already injected; and the 32 tool schemas (169,911 B) outweigh everything else combined.

### 6. Stop nudges: one short line that points at the rule

Replace `REMINDER` (1,841 B) with a one-line capture check of about 300 B that names the trigger and points at the rule the core already serves (`engagement.contract.capture`, and `bootstrap(section=...)` where the client has it): distil, no transcript; capture a durable decision, outcome or stable fact per live policy; supersede rather than correct beside; stated intent goes to Planning and observed outcome to Records; transient code, test and CI output stays out. Replace `EPISODE_ASK` (495 B) with about 330 B that keeps the episode key, the `episode_memory(action="record")` call and "if nothing durable happened, do nothing". Variant A keeps both cadences. Variant B folds the two into one nudge with the same text discipline and fires at most once per episode window (the episode ask's own turn count and cooldown), so the capture check no longer rides every third Stop; the episode ask already asks for the durable record, and the capture loop's rule stays in the core. B changes when an agent is asked, not what it is told; it needs the no-nudge benchmark families to show initiation unchanged (task 3.5) before it ships.

### 7. Retrieval reminder: short, and not repeated

Replace `REMINDER` (861 B) with about 260 B: run a quiet `ask_memory` only when recent context does not already cover the topic and the prompt may touch prior knowledge; cite hits; a miss means not found in that scope; otherwise skip. Variant A keeps the cadence. Variant B emits it once per session and again after a compaction or resume (the checkpoint hook already runs there), so it does not repeat between; at `maximal`, whose contract is recall before every substantive turn, it emits a pointer of about 90 B per prompt (`Recall first: activate_context with the turn.`) instead of silence. The text already tells the agent not to repeat a search because it reappeared; B makes the hook agree.

### 8. Working-set block: silent when unchanged

The packet already carries a `continuity` token. Persist a hash of the rendered item refs beside it and emit nothing when the new packet's refs equal the last emitted set, and a one-line "unchanged since your last turn" only for a referential prompt. Cut the header from 278 to about 120 B and the default ceiling from 4,000 to 2,000 chars on non-referential turns (referential prompts keep 4,000: they are the thread). Opt-in cost is a bound, not a measurement: at most 24 fires per balanced 2-hour session, so up to about 103 KB before and, with a 50% unchanged rate and the smaller ceiling, about 14 KB after.

### 9. Session start and continuation

Model-facing continuation text drops the checkpoint id line and the transcript-binding sha256 line (kept in the checkpoint file and the metadata log, where the hook uses them), and the advisory paragraph shrinks from about 400 to about 150 B. Cap `MAX_CONTEXT_BYTES` at 2,048. Fresh-start injection stays none: no hook injects at `startup`, and this change does not add one. For skill clients, `SKILL.md` (30,175 B, byte-identical in scaffold and plugin) moves its "Before writing" (5,980 B) and "Semantic authoring contract" (8,234 B) sections into `references/` with one-line pointers, leaving about 16 KB; its `Recall loop`, `Proactive engagement`, `Portable operating rules` and `Decision` sections stay.

### 10. Tool schemas

Trim descriptions and per-parameter descriptions in the live registry: tool descriptions to at most about 600 B (`activate_context` 5,479, `edit_memory` 3,961, `remember` 3,494, `observe_memory` 3,156, `replace_memory` 2,966, `manage_memory_file` 2,900 and `maintain_memory` 2,795 are the offenders), parameter descriptions to at most about 80 B each with the prose moved to the `describe` action or a bootstrap section, and defaults not restated. Estimated 170 KB to about 110 KB (-35%). This edits a pinned baseline (`tests/fixtures/mcp_tool_schemas.json`, `test_mcp_schema_fidelity`) and the published tool-surface fingerprint, so it regenerates those and the v5 candidate, and touches nothing in v1 to v4 (legacy schemas are pinned separately). A larger option, a `lean` tool profile exposing about a dozen core tools to coding clients (about 86 KB), changes the tool-surface contract and is not proposed here without a ruling.

### 11. Rules the nudges and hooks must keep

The manifest in decision 3 extends to the hook texts: a compact Stop line must contain the capture trigger (durable decision, outcome or stable fact), the live-policy pointer, distil-not-transcript, supersede-not-correct-beside, and the Planning/Records split; the episode line must contain the `episode_memory` record call, the key and the do-nothing escape; the retrieval line must contain `ask_memory`, cite, the miss-means-not-found-in-scope rule and the skip escape. Hook constants are duplicated between `src/exomem/_hooks/` and `plugins/claude-code/hooks/` (byte-identical, parity test) and the prominence presets are duplicated into the standalone scripts (pinned by `tests/test_prominence.py` and `tests/test_capture_nudge_episode.py`); Phase 2 edits both mirrors together.

### Additional open questions (for the ruling)

5. **Stop cadence.** Variant A (shorter text, same cadence: -80% at balanced) or B (one nudge per episode window: -94%, needs the no-nudge benchmark to confirm initiation)?
6. **Retrieval cadence.** A (same cadence, short text) or B (once per session plus after compaction; a 90-byte pointer per prompt at maximal)?
7. **Tool schemas.** Include the -35% description trim in this change, split it into its own change (it moves a pinned fingerprint), or leave it out? It is the largest single cost.
8. **SKILL.md.** Move "Before writing" and "Semantic authoring contract" into references (30 KB to about 16 KB)?
