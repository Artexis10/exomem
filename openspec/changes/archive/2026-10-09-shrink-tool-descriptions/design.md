## Status

Phase 2 is reconciled with current main, independently reviewed and verified through the installed public interface. Ordinary PR CI, source delivery and external adapter acceptance remain open. Earlier measurements below are historical; current acceptance is recorded in tasks.md.

The reconciled October 4 candidate retains current API and governance contracts and has passed independent source review. Current complete wire is 194,872 → 88,345 bytes, under every original ceiling. Ordinary-agent CLI trials reached the expected outcomes across the eight task shapes, with a cold activation failure handled by targeted retrieval, a filter-shape retry, an exact-unit-reference retry and a completion-value retry. The completion example now appears in the public reference exposed by the existing bootstrap pointer. Provider refresh, native routing acceptance and production deployment remain separate release evidence.

## Agent-independent usability reconciliation (2026-10-04)

Exomem is an evolving epistemic system for agents through its public interfaces, not a feature that requires one provider or private coding harness. Skills may improve workflows, but minimum valid-call guidance must remain discoverable from tools and on-demand public bootstrap/schema reads. This does not promise equal performance from every model.

Reuse this existing change and its size budget. Preserve the newer API-scope, engagement, capture/disclosure, file-handle and authoring contracts from main when shortening descriptions. Keep `PRODUCT_COMMANDS` and canonical leaves as the single source for generated MCP/REST/CLI surfaces. No new routing model, executable nine-action mega-tools or duplicate facade.

Clarify two read entry points: `activate_context` compiles relevant prior context for the current verbatim turn, with bounded conversation context when needed; `ask_memory` retrieves evidence for a known information gap and `read_memory` opens selected results. Neither generates the user's answer. Remove contradictory first-call wording without forcing recall contrary to the saved engagement policy. Retain tool names in this batch. A later rename, split or exposure change needs its own evidence-backed OpenSpec decision and synchronized versioned compatibility; do not advertise synonyms speculatively.

Planning's action-dependent arguments need concise meanings and an inspect/query → guarded update/triage recipe using the actual returned identities and versions. Keep optimistic concurrency guards intact; never invent their values or make them optional to avoid teaching them. Keep raw Sources/Evidence, compiled conclusions, Records observations and Planning intent distinct while explaining that distinction in ordinary language.

Acceptance separates interface failures from compiler failures. Reuse existing isolated task/observation infrastructure for mixed-topic activation, explicit and ambiguous follow-ups, raw preservation, conclusion capture, a structured subscription query, a Planning update and a Planning transition. First verify these workflows and necessary parameter guidance without changing runtime semantics; then retain bounded ordinary-agent traces through the installed public surface, with no private harness hints. Record actual tool selection, arguments/results, retries, answer/source correctness and available usage/latency separately. A forced invocation proves transport, not spontaneous initiation. A rename experiment and paid multi-arm comparison remain separate, not delivery prerequisites. No new benchmark framework or test merely pinning prose bytes.

The outcome is correct, understandable use at lower measured schema size—not a new claim that fewer tools or shorter descriptions alone improve reasoning. Restore any essential guidance a real workflow loses rather than weakening its assertion to meet the byte budget. Record unresolved usability failures explicitly.

## Method

`scripts/measure-tool-schema-bytes.py` builds the server under the same deterministic environment as `scripts/dump-tool-schemas.py` and sizes each tool's wire object as compact UTF-8 JSON. "Total" is what a client receives per tool. The pinned fixture `tests/fixtures/mcp_tool_schemas.json` stores only description and inputSchema, so it understates the surface by the output schemas (17,571 B), annotations, titles and `_meta`.

## Measurements

By component, all 32 tools: total 184,208 B; tool descriptions 36,424 B; input schemas 121,419 B, of which parameter descriptions 58,901 B and about 62 KB is structure (types, defaults, enums, property names); output schemas 17,571 B (16,045 B of it is `ask_memory`); annotations, title and `_meta` about 6 KB.

Structure inside the input schemas: `anyOf` null wrappers 11,258 B over 433 sites; `"default": null` 7,259 B over 427 sites; other defaults 2.6 KB; property names 9.7 KB across 548 top-level parameters; enums 2.0 KB; `additionalProperties` 2.3 KB.

Parameter descriptions are mostly short (386 of 520 are under 100 B). The bulk is the semantic-authoring copies, a few multiplexer parameters, and the shared blocks below.

| Tool | Total now | Description | Input schema | of which param descriptions | Output schema | Mechanical cut (A+B+C+D) | Target |
|---|---:|---:|---:|---:|---:|---:|---:|
| `ask_memory` | 22,406 | 444 | 5,662 | 2,568 | 16,045 | 16,922 | 4,200 |
| `manage_memory_file` | 13,600 | 2,900 | 10,375 | 7,449 | 45 | 8,196 | 3,600 |
| `edit_memory` | 13,209 | 3,961 | 8,915 | 3,037 | 45 | 5,969 | 4,800 |
| `remember` | 12,452 | 3,494 | 8,649 | 4,948 | 45 | 5,653 | 4,500 |
| `replace_memory` | 10,986 | 2,966 | 7,702 | 3,925 | 45 | 5,696 | 3,700 |
| `activate_context` | 9,760 | 5,495 | 3,830 | 2,927 | 45 | 374 | 4,200 |
| `connect_memory` | 7,774 | 332 | 6,951 | 2,695 | 221 | 1,664 | 4,300 |
| `maintain_memory` | 7,558 | 2,803 | 4,385 | 1,723 | 45 | 890 | 4,300 |
| `schema_memory` | 7,484 | 328 | 6,849 | 4,363 | 45 | 931 | 4,200 |
| `record_memory` | 7,425 | 387 | 6,731 | 2,280 | 45 | 1,492 | 4,300 |
| `observe_memory` | 7,400 | 3,156 | 3,924 | 1,968 | 45 | 2,542 | 3,500 |
| `episode_memory` | 6,941 | 1,174 | 5,444 | 2,185 | 45 | 890 | 4,200 |
| `review_memory` | 6,619 | 416 | 5,889 | 4,323 | 45 | 589 | 4,000 |
| `adoption_studio` | 5,519 | 1,121 | 4,073 | 1,944 | 45 | 632 | 3,500 |
| `capture_source` | 5,301 | 799 | 4,182 | 2,158 | 45 | 546 | 3,600 |
| `govern_memory` | 5,283 | 280 | 4,697 | 1,818 | 45 | 1,105 | 3,300 |
| `triage_memory` | 4,538 | 218 | 4,013 | 1,694 | 45 | 347 | 3,000 |
| `plan_memory` | 4,362 | 803 | 3,249 | 196 | 45 | 1,449 | 2,500 |
| `preserve_artifacts` | 3,072 | 714 | 1,998 | 891 | 45 | 159 | 2,200 |
| `configure_memory` | 2,409 | 755 | 1,329 | 634 | 45 | 245 | 1,700 |
| `review_item_context` | 2,334 | 362 | 1,647 | 812 | 45 | 202 | 1,500 |
| `read_memory` | 2,212 | 498 | 1,411 | 727 | 45 | 202 | 1,600 |
| `adopt_vault` | 2,206 | 194 | 1,712 | 629 | 45 | 202 | 1,500 |
| `query_dataset` | 2,042 | 199 | 1,538 | 328 | 45 | 460 | 1,400 |
| `preserve_evidence` | 2,023 | 539 | 1,160 | 553 | 45 | 159 | 1,500 |
| `bootstrap` | 1,936 | 658 | 979 | 548 | 45 | 202 | 1,400 |
| `process_media` | 1,632 | 373 | 951 | 370 | 45 | 202 | 1,200 |
| `transfer_artifact` | 1,484 | 319 | 845 | 434 | 45 | 116 | 1,100 |
| `browse_memory` | 1,340 | 186 | 845 | 302 | 45 | 116 | 1,000 |
| `read_media` | 1,102 | 176 | 691 | 216 | 0 | 202 | 800 |
| `compile_source` | 1,046 | 213 | 524 | 161 | 45 | 159 | 750 |
| `coordination_status` | 753 | 161 | 269 | 95 | 45 | 116 | 600 |
| **Total** | **184,208** | **36,424** | **121,419** | **58,901** | **17,571** | **58,629** | **87,950** |

Sorted by current total. "Mechanical cut" is the simulated saving from items 1 to 4 below, applied to the live wire objects. "Target" is the per-tool ceiling proposed for Phase 2.

### Duplicated prose

| Block | Copies | Bytes each | Total |
|---|---:|---:|---:|
| Semantic authoring contract (5 tool descriptions, 5 parameter descriptions) | 10 | about 2,480 | 24,800 |
| `authorization_session_credential` property (type, null arm, default, description) | 32 | about 210 | 6,720 |
| `response_detail` property | 21 | about 211 | 4,441 |
| `purpose` description | 4 | about 316 | 1,262 |

Parameters whose description restates the type or enum: `review_memory.mode` (1,836 B listing 18 modes that the `enum` already carries), `manage_memory_file.operation` (lists nine values the enum carries), `govern_memory.operation`, `triage_memory.action`, `schema_memory.subject`. Several `Optional ...` prefixes and "Optional short rationale" phrasings restate that a parameter is not `required`.

Long guidance that belongs elsewhere: `activate_context` (5,479 B, of which the abstention taxonomy, `carried_by` values, `retrieval_carried` versus `retrieval_named` and the upkeep/episode_due carriers are a reference manual), `maintain_memory` (2,795 B, five mode sub-manuals), `adoption_studio` lifecycle, `schema_memory.operation` (1,531 B matrix), `review_memory` per-mode parameter notes. There are no long worked examples in the schemas except the semantic-authoring role example, which stays once.

## Decisions

### 1. One compact semantic-authoring rule, five copies

Each of the five authoring tools keeps a single ~740 B paragraph: the identity marker `[exomem.semantic-authoring:v4 sha256:...]` (unchanged), the minimum (one non-empty unit for active compiled notes on create, replace or draft-to-active; inactive drafts exempt; the last unit cannot be removed), the compact syntax, the rich heading form, the two refusal codes with their one-line remediation, and the pointer to `bootstrap(profile="full")` for core keys and aliases. Parameter descriptions keep their leading sentence and drop the contract. The full text stays in bootstrap, `docs/semantic-language.md`, and the skill. `render_concise` and `workflow_skills.py` consume the same contract and are unaffected. Saves about 21.7 KB.

Behaviour-critical and staying: the refusal codes and remediation, because a hosted agent with no skill sees only the schema when a write is refused; the "inactive drafts exempt" clause, because it is what stops agents inventing units for drafts.

### 2. Loose `ask_memory` output schema

Declare `ask_memory` with the same shape family as `read_memory` (an object with `additionalProperties: true`), keeping the `result` wrap and `x-fastmcp-wrap-result` so `structured_content["result"]` is byte-identical at runtime. Saves 15,950 B. This deliberately supersedes assertions in `tests/test_portable_retrieval.py` and `tests/test_retrieval_surface_safety.py` that pin the union arms and the presence of `retrieval_profile`, `ranking_explanation`, `unit_ref` and `parent_path` in the published schema. Those tests are updated to assert the runtime payload carries those fields instead. Output schemas are advisory for clients; nothing in Exomem branches on them. A middle option keeps named top-level fields typed (about 3 KB); see the ruling.

### 3. Drop optional-null structure

Publish optional parameters as their base type without `"default": null` and without the null arm. Server-side validation is built from the Python signature, not from the published JSON, so an explicit `null` from a client should still be accepted; Phase 2 adds a red test that proves it for a sample of tools before the schema stops advertising it. Saves 18,642 B. The risk is a strict client validator that rejects nulls the schema no longer lists; that only bites a client that sends null for an unset optional, which the published schema no longer invites. Required-ness is unchanged.

### 4. Shared parameters defined once

`authorization_session_credential` stays declared (the wrapper raises `AuthorizationContextUnavailable` if it is ever set, which is a guard) but its description becomes "Reserved; leave unset." Saves 2,336 B beyond item 3. `response_detail` and `purpose` become one short shared description each, with the default kept in the `default` field only (about 3 KB across 25 sites, counted in the per-tool targets).

### 5. Prose diet for the remainder

Per tool, in order of size. Targets are ceilings in the table above; the rewrite rules are: state the purpose and the choice against neighbouring tools in the first paragraph; keep every refusal code, guard flag and destructive-operation requirement that an incident depends on; delete enum lists and type restatements from parameter descriptions; move sub-manuals out.

| Tool | What stays in the schema | What moves |
|---|---|---|
| `activate_context` | call once per substantive turn with the user's words verbatim; returns a bounded packet; abstains rather than guesses; pass `anchor` to disambiguate; `continuity`/`session`/`workspace` roles; read-only | abstention reasons, `carried_by` and `status` value catalogue, `recent_context` semantics, `episode_due` and `upkeep` carriers go to `references/recall.md` |
| `maintain_memory` | modes and their defaults (audit read-only; fix/backfill dry-run; reconcile writes); remote write modes are operator-only (`MAINTENANCE_REQUIRES_CLI`) | `structured-files`, `curation`, `tag-variants` and sidecar-collapse manuals go to `references/vault-care.md` |
| `edit_memory` | one auditable reason; the `validate_only` then `transition_token` relation-disposition recovery (incident-critical); `## Relations` syntax | source-citation audit detail to `references/writing.md` |
| `remember`, `replace_memory`, `manage_memory_file` | lane choice (Source vs Evidence vs compiled); `sources` must resolve to governed material or be an honest empty list (`UNRESOLVED_SOURCE_CITATION`) | back-reference mechanics to `references/writing.md` |
| `schema_memory`, `review_memory`, `govern_memory` | required-field pairings and the guard fields (`expected_hash`, `expected_fingerprint`) | per-mode matrices to `references/governance.md` and `operation-routing.md` |
| `adoption_studio`, `episode_memory`, `capture_source` | lifecycle action names and the never-modify-originals guarantee; record once at a decision point; classification is optional | lifecycle narrative and leaf semantics to `references/operations.md` |

Moves land in the hand-authored generic scaffold (`src/exomem/_scaffold/_Schema/references/`), stay free of personal tokens (`tests/test_scaffold_no_leak.py`), and are reachable to hookless clients through a single one-line pointer in each shortened description and a reference index in `bootstrap(profile="full")`. Compact bootstrap is not enlarged.

## Budget and fingerprint

Proposed budget: total wire bytes at most **90,000** (-51.1%), per-tool ceilings as in the table (they sum to 87,950 B, leaving about 2 KB of slack for the shared blocks). Without item 3 the same prose cuts land at about 106,600 B (-42%); reaching -50% without it needs about 14.6 KB more prose removed, which starts cutting rules. That is why item 3 is a ruling, not a default.

The tool-surface fingerprint (`src/exomem/tool_surface_contract.json`, hashed over name, title, description, inputSchema, outputSchema, icons, annotations, meta, execution) moves once, in the final Phase 2 commit, so an intermediate commit never carries a half-changed hash. `deploy/chatgpt/personal-plugin-contract.json` records an external attestation, not the locally generated surface: never replace its registered digest without fresh connector evidence. A cached external adapter remains refresh/verification-pending until it exposes the released surface and passes its own acceptance; that state does not block independently verified MCP, CLI or REST delivery. Reconcile any generator message or gate that incorrectly makes the external refresh a product-release prerequisite. Refresh automatically through the supported adapter mechanism where available; request an owner action only when the provider offers no authorized automation. After release, verify discovery, retrieval and an authorized isolated capture through that adapter before updating its attested digest. Do not create a production write merely to smoke the description change.

## Frozen and derived artifacts

- Hosted v1 to v4: resolve pinned legacy schemas; `test_hosted_legacy_profile_pin.py` and `tests/fixtures/hosted_v1_v4_immutability_manifest.json` (30 files) must stay green and byte-identical. No edit to `hosted_legacy_profile_schemas.json`.
- `hosted-alpha-agent-v4-command-binding-v1`: reuses the v4 profile, so it inherits the pin and is expected not to move. Phase 2 verifies its lock and compatibility files are unchanged rather than regenerating them.
- Hosted v5 (`plugins/hosted/generated/candidates/hosted-alpha-agent-v5`, `compatibility.json` about 238 KB) renders from the live registry and is regenerated with `scripts/hosted-plugin.py regenerate --candidate hosted-alpha-agent-v5`, checked with `check`.
- Also regenerated: `tests/fixtures/mcp_tool_schemas.json` and `src/exomem/tool_surface_contract.json` (`scripts/dump-tool-schemas.py`), `plugins/claude-code` skills where scaffold references change, the current generated hosted render (`plugins/hosted/generated/compatibility.json`), `docs/capabilities.md` (`scripts/generate-capabilities.py`), and `tests/harness_modules.txt` only if a new test module imports `benchmarks/`.

## Test plan

Existing, must pass after regeneration: `test_mcp_schema_fidelity.py` (byte fidelity against the regenerated fixture), `test_tool_surface_contract.py`, `test_tool_surface_fingerprint.py`, `test_hosted_legacy_profile_pin.py`, `test_hosted_v5_candidate.py`, `test_hosted_agent_surface.py` (output-schema presence), `test_semantic_authoring_contract.py`, `test_workflow_skills.py`, `test_scaffold_no_leak.py`, and the plugin/hosted rendering suites. The output-schema conformance check runs `ask_memory` and `read_memory` against the vault fixture and validates `structured_content` against the published schema.

Changed on purpose: `test_portable_retrieval.py` and `test_retrieval_surface_safety.py` move their union-arm assertions from the published schema to the runtime payload.

New, written red-first (fails on `main` at 184,208 B): `tests/test_tool_schema_budget.py` pins total wire bytes at or under the ruled budget and each tool at or under its ceiling, and asserts the semantic-authoring contract appears in exactly the five authoring tool descriptions, once each, with its identity marker, and in no parameter description. It shares `size()` with `scripts/measure-tool-schema-bytes.py` so the script and the test cannot disagree.

Behavioural check for cut guidance: the hosted behaviour fixtures and `test_hosted_plugin_behavior.py` run unchanged; any case that depended on a removed sentence is a reason to put the sentence back, not to edit the fixture.

## Risks

- Hosted agents without skills lose reference prose. Mitigation: refusal codes and guard fields stay in place; the pointer and the reference index cover the rest; the behaviour fixtures gate it.
- A client that validates strictly against published output schemas loses typed `ask_memory` results. Mitigation: runtime payload is unchanged; ruling 2 offers the middle option.
- Cached external adapters need a supported refresh and their own acceptance after a fingerprint move. Automate it where supported; remaining owner-only provider steps do not hold independently verified product surfaces.

## Rulings

1. Budget: at most 90,000 wire bytes, with per-tool ceilings, enforced by `tests/test_tool_schema_budget.py`.
2. `ask_memory` output schema: the loose wrapped object that keeps the `result` wrap; union-arm assertions move to runtime-payload assertions. The failure-envelope prerequisite from #1448 is already integrated by #1477 (7fd5429aa): current main's `RecallResult` includes `ToolFailureEnvelope`. Do not wait for or re-merge the historical #1448 branch; preserve the current runtime failure and frozen-profile contracts.
3. Optional-null structure: dropped, on the condition that the server keeps accepting an explicit null for every optional parameter; a test sends null for every nullable optional parameter of every tool.
4. `authorization_session_credential`: stays declared with a one-line description.
5. `hosted-alpha-agent-v4-command-binding-v1` and every frozen v1 to v4 byte stay unchanged.

## Outcome

Measured with `scripts/measure-tool-schema-bytes.py` on the same basis as Phase 1: **184,208 B to 89,562 B (-51%)** across the 32 tools (the base at the merge point was 188,530 B after later `main` changes). Per tool:

| Tool | Before (0.97.0) | After | Change |
|---|---:|---:|---:|
| `ask_memory` | 22,406 | 4,112 | -82% |
| `manage_memory_file` | 13,600 | 4,321 | -68% |
| `edit_memory` | 13,209 | 6,721 | -49% |
| `remember` | 12,452 | 5,177 | -58% |
| `replace_memory` | 10,986 | 4,590 | -58% |
| `activate_context` | 9,760 | 3,279 | -66% |
| `connect_memory` | 7,774 | 4,288 | -45% |
| `maintain_memory` | 7,558 | 3,820 | -49% |
| `schema_memory` | 7,484 | 3,982 | -47% |
| `record_memory` | 7,425 | 4,265 | -43% |
| `observe_memory` | 7,400 | 3,848 | -48% |
| `episode_memory` | 6,941 | 4,283 | -38% |
| `review_memory` | 6,619 | 3,371 | -49% |
| `adoption_studio` | 5,519 | 3,400 | -38% |
| `capture_source` | 5,301 | 3,385 | -36% |
| `govern_memory` | 5,283 | 3,012 | -43% |
| `triage_memory` | 4,538 | 3,091 | -32% |
| `plan_memory` | 4,362 | 2,384 | -45% |
| `preserve_artifacts` | 3,072 | 2,300 | -25% |
| `configure_memory` | 2,409 | 1,685 | -30% |
| `review_item_context` | 2,334 | 1,487 | -36% |
| `read_memory` | 2,212 | 1,595 | -28% |
| `adopt_vault` | 2,206 | 1,479 | -33% |
| `query_dataset` | 2,042 | 1,358 | -33% |
| `preserve_evidence` | 2,023 | 1,483 | -27% |
| `bootstrap` | 1,936 | 1,390 | -28% |
| `process_media` | 1,632 | 1,187 | -27% |
| `transfer_artifact` | 1,484 | 1,084 | -27% |
| `browse_memory` | 1,340 | 992 | -26% |
| `read_media` | 1,102 | 797 | -28% |
| `compile_source` | 1,046 | 813 | -22% |
| `coordination_status` | 753 | 583 | -23% |
| **Total** | **184,208** | **89,562** | **-51%** |

Where the bytes went: optional-null structure and `additionalProperties: true` about 19 KB; the ten semantic-authoring copies (24.8 KB) became five bounded rules (`render_tool_guidance`, two tiers: whole-page writers carry the category selection rule and example, `observe_memory`, `edit_memory` and `manage_memory_file` carry the minimum, lifecycle, final-unit rule and remediation); `ask_memory`'s output schema 16.5 KB to 139 B; per-tool prose and parameter descriptions the rest. Long manuals moved to `references/{recall,writing,supersession,governance,vault-care,operation-routing,operations,planning-records}.md` in the generic scaffold.

Implementation notes that differ from the proposal:

- Compaction is one function, `command_surface.compact_input_schema`, applied once after registration for the personal MCP surface, to the REST/OpenAPI request schemas (so REST and MCP keep publishing identical parameter schemas), and to the hosted contract for non-legacy profiles. Historical profiles are skipped.
- `hosted-alpha-agent-v4-command-binding-v1` reuses the v4 profile, whose gateway contract now carries the released `ask_memory` output schema from `hosted_legacy_ask_output_schema.json`. Its compatibility bytes and its command-binding contract digest are therefore unchanged. Only v5 follows the shortened live schema.
- REST/OpenAPI and CLI `--help` no longer repeat the semantic-authoring contract on parameters (the write tools' MCP descriptions carry it once); `bootstrap(profile="full")` remains the full source.
- A rule the trim once dropped and the tests caught was restored: `edit_memory`'s `relation_review_hash=<returned relation_review_hash>` recipe and `## Relations` example, `manage_memory_file`'s `draft_token` pairing, the open-vocabulary note on `source_type`, `suggestions=true`, and the `rebuild_graph` quarantine wording.
