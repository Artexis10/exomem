# Capabilities

This file is generated from `src/exomem/commands.py`.
Run `uv run python scripts/generate-capabilities.py` to refresh it.
Run `uv run python scripts/generate-capabilities.py --check` to verify it is current.

## Summary

- Product commands: 22
- Tier 1 commands: 19
- Tier 2 commands: 3
- Registry-generated MCP commands: 22
- REST commands: 21
- CLI commands: 21
- Hand-registered MCP tools: none

## Product Command Registry

| Command | Tier | Surfaces | Mode | Destructive | CLI positional | Routes | Parameters | Summary |
| --- | ---: | --- | --- | --- | --- | --- | --- | --- |
| coordination_status | 1 | MCP, REST, CLI | read | no | - | coordination_status | - | Report this replica's writer-lease role and coordinator health. |
| bootstrap | 1 | MCP, REST, CLI | read | no | - | bootstrap | profile, workflow | Return Exomem's versioned operating contract for generic MCP clients. |
| ask_memory | 1 | MCP, REST, CLI | read | no | query | search, find | query, types, projects, tags, speakers, file_types, exclude_file_types, limit, scope, mode, detail, deep, graph, rerank, prefer_compiled, prefer_active, prefer_used, graph_enrich, include_timings | Recall durable knowledge from Exomem with product defaults. |
| read_memory | 1 | MCP, REST, CLI | read | no | path | fetch, get | path*, frontmatter_only, include_history, links, include_raw | Read one memory page or curated vault file by path. |
| browse_memory | 1 | MCP, REST, CLI | read | no | path | overview, list_directory | path, mode, max_depth, include_hidden, samples, recursive | Browse vault structure without reading many files. |
| remember | 1 | MCP, REST, CLI | write | no | - | note | content*, title*, note_type, project, projects, sources, tags, status, severity, pattern_type, domain, started, duration, hypothesis, n, concluded, medium, recorded, published, host, editor, suggestions, project_category | Remember a durable conclusion as compiled governed knowledge. |
| edit_memory | 1 | MCP, REST, CLI | write | yes | path | edit | path*, why*, new_body, tags, old_string, new_string, replace_all, heading, section_position, edits, row_key, take, overwrite, field, value, allow_curated, expected_hash, validate_only | Edit an existing memory page with an auditable reason. |
| replace_memory | 1 | MCP, REST, CLI | write | yes | old_path | replace | old_path*, content*, title*, note_type, reason, project, projects, sources, tags, status, severity, pattern_type, domain, started, duration, hypothesis, n, concluded, medium, recorded, published, host, editor, project_category | Supersede an existing compiled memory with a new version. |
| capture_source | 1 | MCP, REST, CLI | write | no | - | add, propose_compilation | content*, title*, source_type, url, tags, why_captured, compile_guidance, suggested_title | Capture raw source material and optionally return compile guidance. |
| compile_source | 1 | MCP, REST, CLI | read | no | - | propose_compilation | sources*, suggested_title | Plan a compiled note from one or more raw sources. |
| preserve_evidence | 1 | MCP, REST, CLI | write | no | - | preserve | scope*, category*, filename*, content*, description | Preserve text evidence as append-only proof material. |
| transfer_artifact | 1 | MCP, REST, CLI | write | no | - | transfer_token | operation | Prepare out-of-band binary artifact transfer. |
| review_memory | 1 | MCP, REST, CLI | read | no | - | attention, audit, evolution, provenance_report, propose_compilation | mode, categories, limit, query, sources, suggested_title, tag, key, value, path, state, ref | Review memory health, provenance, drift, or source backlog. |
| review_item_context | 1 | MCP, REST, CLI | read | no | ref | review_item_context | ref*, expected_fingerprint, max_body_chars, max_related_pages, max_graph_nodes, max_graph_edges, max_history, max_evolution_versions | Inspect one stable review item with bounded recorded context. |
| triage_memory | 1 | MCP, REST, CLI | write | no | ref | attention | ref*, action*, until, why, expected_fingerprint | Triage one Epistemic Inbox item explicitly. |
| connect_memory | 1 | MCP, REST, CLI | write | no | - | suggest_links, graph_context, suggest_relations, link, list_inbound_links | operation, path, target, query, draft_title, draft_body, limit, scope, include_model_suggestions, depth, relation_types, node_types, max_nodes, max_edges, traversal_profile, max_body_chars, entity_type, name, summary, why_in_kb, tags, connections, affiliation, relationship, domain, language, repo, license, used_in, decided, project, decision_status, ref, expected_hash, why, expected_fingerprint | Connect memory through links, typed graph context, or entities. |
| adopt_vault | 1 | MCP, REST, CLI | write | no | path | adopt | path, mode, max_depth, include_hidden, samples, pack_limit, manifest_path, selected_paths | Adopt an existing vault safely without replacing originals. |
| maintain_memory | 1 | MCP, REST, CLI | write | yes | - | audit, audit_fix, reconcile | mode, categories, dry_run, rebuild_embeddings | Maintain vault health with explicit write-capable modes. |
| schema_memory | 1 | MCP, REST, CLI | write | yes | - | schema_memory | operation*, name, subject, project, page_type, save, expected_hash, strict, compare_to, proposal, include_model_suggestions | Infer, validate, diff, or save governed memory schemas. |
| manage_memory_file | 2 | MCP, REST, CLI | write | yes | - | create_file, list_directory, move_file, delete, append_to_file, list_trash, recover_from_trash | operation, path, content, frontmatter, overwrite, allow_curated, kind, parents, recursive, include_hidden, old_path, new_path, update_wikilinks, confirm, force_orphan, force_superseded, expected_dead_inbound, trash_path, restore_path, date | Manage files through one governed file operation. |
| query_dataset | 2 | MCP, REST, CLI | read | no | path | query_data | path*, record_path, filters, columns, sort_by, descending, limit, offset, aggregate, date_from, date_to, date_column | Query a CSV, TSV, or JSON dataset under the vault. |
| read_media | 2 | MCP | read | no | path | get_video_frames | path*, max_frames, start_sec, end_sec | Read sampled video frames inline for visual inspection. |

## Hand-registered MCP Tools

`HAND_REGISTERED_EXCEPTIONS` lists product tools that cannot be generated by the generic MCP registry loop.
The default product surface currently has no hand-registered MCP exceptions.
Artifact transfer is exposed through `transfer_artifact`; canonical token helpers remain implementation details.

## Notes

- A `*` suffix in the parameter list means the parameter is required.
- Tier 2 commands are advanced file and data operations exposed only when the surface enables tier 2.
- Destructive commands are writes that can replace, move, delete, or bulk-fix content.
