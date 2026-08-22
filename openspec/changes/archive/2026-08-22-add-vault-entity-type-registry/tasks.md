## 1. Registry Core (Red-Green)

- [x] 1.1 `test_extension_type_loads_beside_core`
- [x] 1.2 `test_extension_id_folder_alias_collisions_with_core_are_findings_not_exceptions`
- [x] 1.3 `test_invalid_folder_segment_is_a_finding`
- [x] 1.4 `test_deprecated_extension_is_excluded_from_active_ids`
- [x] 1.5 `test_parent_must_name_a_core_type`
- [x] 1.6 `test_loader_is_cached_by_extension_hash`
- [x] 1.7 `test_save_registry_refuses_observed_deletion_and_stale_hash`
- [x] 1.8 `test_cue_nouns_default_to_aliases`
- [x] 1.9 Implement `EntityTypeRegistry`, extension validation, hashing/cache, and guarded atomic save while preserving core-only compatibility symbols

## 2. Runtime Consumers (Red-Green)

- [x] 2.1 `test_registry_enumerates_extension_type_folders`
- [x] 2.2 `test_cue_fires_for_vault_defined_type_noun`
- [x] 2.3 `test_vault_defined_type_restricts_candidates_to_that_type`
- [x] 2.4 `test_referents_resolve_a_vault_defined_entity_type_end_to_end`
- [x] 2.5 `test_create_entity_accepts_vault_defined_type_and_creates_its_folder_lazily`
- [x] 2.6 `test_create_entity_rejects_unknown_type_naming_active_ids`
- [x] 2.7 `test_resolve_entity_scopes_to_vault_defined_type`
- [x] 2.8 `test_entity_index_includes_extension_folders_and_rebuilds_on_registry_change`
- [x] 2.9 Migrate initialization, indexes, create/resolve, candidates, entity enumeration, and referent cue/runtime consumers to the vault-aware registry

## 3. Attention, Bootstrap, Packs, And Latency (Red-Green)

- [x] 3.1 `test_unregistered_entity_type_is_an_attention_finding_with_proposed_entry`
- [x] 3.2 `test_unregistered_type_finding_resolves_when_the_type_is_registered`
- [x] 3.3 `test_three_pages_under_an_unregistered_folder_trigger_the_finding_two_do_not`
- [x] 3.4 `test_entity_capture_types_include_vault_defined_types`
- [x] 3.5 `test_default_entity_types_accept_vault_defined_ids`
- [x] 3.6 `test_entity_type_registry_load_is_bounded_at_scale`
- [x] 3.7 Add deterministic unregistered-type audit/attention composition and migrate bootstrap, knowledge-pack, and adoption guidance consumers

## 4. Governed Surface, Documentation, And Generated Contracts

- [x] 4.1 Add `save-entity-types` beside the relation-registry save leaf and runtime `ENTITY_TYPE_UNKNOWN` validation for free-string entity types
- [x] 4.2 Document `_Schema/entity-types.yaml` and governed capture guidance in the scaffold using only synthetic examples
- [x] 4.3 Regenerate the Claude Code plugin mirror with `exomem package-skills --plugin-root plugins/claude-code`
- [x] 4.4 Regenerate MCP schema and tool-surface fixtures through repository generators
- [x] 4.5 Regenerate capability docs through `scripts/generate-capabilities.py`
- [x] 4.6 Keep `test_mcp_schema_fidelity.py`, `test_plugin_sync.py`, `test_scaffold_no_leak.py`, and `test_entity_capture_scaffold.py` green
- [x] 4.7 Add only required lint/mypy allowlist lines to `.github/workflows/ci.yml`

## 5. Required Verification Gates

- [x] 5.1 Run the focused shard including every named test file, referent benchmark tests, governance egress, CLI core ops, and source taxonomy
- [x] 5.2 Run `uv run --frozen python -m pytest tests/test_latency_gate.py -q`
- [x] 5.3 Run `uv run --frozen python scripts/referent_resolution_benchmark.py --check`
- [x] 5.4 Run `uv run --frozen python scripts/semantic_write_latency.py --check`
- [x] 5.5 Run `uvx ruff check . --select F`, strict targeted ruff, and targeted mypy
- [x] 5.6 Run `uv run --frozen python scripts/generate-capabilities.py --check`
- [x] 5.7 Run `npm exec --yes @fission-ai/openspec -- validate --specs --strict`
- [x] 5.8 Run `EXOMEM_DISABLE_EMBEDDINGS=1 uv run --frozen python -m pytest -q --timeout=120 -p no:cacheprovider tests` without `-x`
- [x] 5.9 Review the final diff for scope, private identifiers, generated-file discipline, and untouched forbidden paths
