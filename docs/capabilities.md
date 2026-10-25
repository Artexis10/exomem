<!-- authority:non-specification -->

# Capabilities

This file is generated from `src/exomem/commands.py`.
Run `uv run python scripts/generate-capabilities.py` to refresh it.
Run `uv run python scripts/generate-capabilities.py --check` to verify it is current.

## Summary

- Product commands: 35
- Tier 1 commands: 31
- Tier 2 commands: 4
- Registry-generated MCP commands: 33
- REST commands: 32
- CLI commands: 32
- Hand-registered MCP tools: none

## Hosted Cell Capability Boundary

Hosted operation keeps this registry-derived command surface inside one private,
single-vault cell per tenant. The public gateway authenticates the account, resolves
exactly one cell from server-side state, checks provider-neutral entitlements, and then
forwards a compatible registry command. Public callers cannot select a tenant, cell,
private endpoint, credential, or vault path.

Exomem cells own command execution, mutation safety, private readiness, bounded feature
enforcement, and canonical export/restore. They do not own accounts, public sessions,
Paddle, provisioning, encrypted backup retention, KMS, or destructive external deletion.
Paddle is never a cell request-path or package runtime dependency; Substrate projects its
billing state into internal provider-neutral capabilities before routing.

See the Exomem Cloud runbooks in [runbooks/](runbooks/) and the
[Substrate control-plane contract](substrate-control-plane-contract.md).

## Product Command Registry

| Command | Tier | Surfaces | Mode | Destructive | CLI positional | Routes | Parameters | Summary |
| --- | ---: | --- | --- | --- | --- | --- | --- | --- |
| configure_memory | 1 | MCP, REST, CLI | write | yes | - | configure_memory | action, prominence, expected_revision, context, response_detail | Inspect or change saved Exomem engagement for this vault. |
| coordination_status | 1 | MCP, REST, CLI | read | no | - | coordination_status | - | Read writer-lease health during outages. |
| bootstrap | 1 | MCP, REST, CLI | read | no | - | bootstrap | profile, workflow, skill_contract, section | Read Exomem's contract and live settings when missing or changed; reuse while current. |
| search_memory | 1 | MCP, REST, CLI | read | no | query | search, find | query, types, projects, tags, speakers, file_types, exclude_file_types, categories, kinds, source_kinds, domains, relations, relation_of, relation_direction, filters, result_level, limit, continuation, scope, mode, detail, deep, graph, rerank, rerank_max_candidates, prefer_compiled, prefer_active, prefer_used, widen_outside_kb, graph_enrich, include_timings, explain, purpose | Search compiled knowledge and originals for an evidence gap. |
| recall_context | 1 | MCP, REST, CLI | read | no | turn | activate_context | turn, max_chars, purpose, continuity, anchor, include_timings, client, session, workspace, conversation, trigger, initiator | Recall bounded authorized context; abstain if uncertain. |
| read_memory | 1 | MCP, REST, CLI | read | no | path | fetch, get | path*, frontmatter_only, include_history, links, include_raw, unit_ref, purpose, history_limit, history_continuation, continuation | Read a selected page, semantic unit or view. |
| browse_memory | 1 | MCP, REST, CLI | read | no | path | overview, list_directory | path, mode, max_depth, include_hidden, samples, recursive, limit, cursor | Browse folders; read-only. |
| viewer_map | 1 | MCP, REST, CLI | read | no | - | viewer_map | max_nodes, max_edges, purpose | Return a bounded global map of admitted current graph pages. Read-only. |
| create_note | 1 | MCP, REST, CLI | write | no | - | note | content*, title*, slug, note_type, project, projects, sources, tags, status, severity, pattern_type, domain, started, duration, hypothesis, n, concluded, medium, recorded, published, host, editor, bridge_of, bridge_scope, bridge_review, suggestions, project_category, validate_only, draft_id, draft_hash, draft_token, relation_disposition, relation_review_hash, relation_review_reason, vocabulary_decision, summary, topics, response_detail | Save a durable conclusion as compiled knowledge. |
| edit_note | 1 | MCP, REST, CLI | write | yes | path | edit | path*, why*, operation*, validate_only, identity_decision, response_detail | Edit an existing memory page with an auditable reason. |
| observe_memory | 1 | MCP, REST, CLI | write | yes | path | observe_memory | path*, operation, category, content, kind, tags, context, relations, verdict, check_by, id, unit_ref, expected_fingerprint, expected_hash, transition_token, relation_disposition, relation_review_hash, relation_review_reason, reason, target_fingerprints, response_detail | Check or edit one semantic unit on a compiled note. |
| supersede_note | 1 | MCP, REST, CLI | write | yes | old_path | replace | old_path*, content*, title*, slug, note_type, reason, project, projects, sources, tags, status, severity, pattern_type, domain, started, duration, hypothesis, n, concluded, medium, recorded, published, host, editor, bridge_of, bridge_scope, bridge_review, project_category, validate_only, draft_id, draft_hash, draft_token, relation_disposition, relation_review_hash, relation_review_reason, vocabulary_decision, response_detail | Supersede compiled memory with a new version. |
| capture_source | 1 | MCP, REST, CLI | write | no | - | add, propose_compilation | title*, content, slug, source_type, url, tags, why_captured, compile_guidance, suggested_title, source_kind, domain, projects, files, adoption, raw_protection, source_identity, response_detail | Save raw text or file handles losslessly in Sources/. |
| episode_memory | 1 | MCP, REST, CLI | write | yes | - | episode_memory | action*, episode, subject, summary, worked_on, decided, open, said, about, client, candidate, proposal, disposition, reason, input_revision, journal_digest, order, max_leaves, postcommit, response_detail | Save a recap for later sessions. |
| prepare_note | 1 | MCP, REST, CLI | read | no | - | propose_compilation | sources*, suggested_title | Prepare a sourced note without saving. |
| preserve_evidence | 1 | MCP, REST, CLI | write | no | - | preserve | scope*, category*, filename*, content*, description, raw_protection, response_detail | Preserve text as append-only proof. |
| preserve_artifacts | 1 | MCP, REST, CLI | write | no | - | preserve | scope*, category*, files*, adoption, transcriptions, raw_protection, response_detail | Preserve client file handles as append-only Evidence. |
| transfer_artifact | 1 | MCP, REST, CLI | write | no | - | transfer_token | operation, lane, artifact, expected_sha256, response_detail | Prepare binary transfer when client file handles are unavailable. |
| process_media | 1 | MCP, REST, CLI | write | no | - | - | path, paths, operation, response_detail | Check governed media; retry after remediation. |
| review_memory | 1 | MCP, REST, CLI | write | no | - | attention, audit, evolution, provenance_report, propose_compilation | mode, categories, limit, continuation, query, sources, suggested_title, tag, key, value, path, state, ref, detail, legacy_sample_limit, family, response_detail | Review health, provenance, drift and backlog. |
| review_item_context | 1 | MCP, REST, CLI | read | no | ref | review_item_context | ref*, expected_fingerprint, max_body_chars, max_related_pages, max_graph_nodes, max_graph_edges, max_history, max_evolution_versions, continuation | Read one review item's target, provenance, relations and history. |
| triage_memory | 1 | MCP, REST, CLI | write | no | ref | attention | ref*, action*, until, why, expected_fingerprint, source_path, decision, response_detail | Save review decisions or attention settings; use returned refs and fingerprints. |
| connect_memory | 1 | MCP, REST, CLI | write | no | - | suggest_links, graph_context, suggest_relations, link, list_inbound_links | operation, path, target, query, requested_relation, continuation, unit_ref, categories, kinds, draft_title, draft_body, limit, scope, include_model_suggestions, depth, relation_types, node_types, max_nodes, max_edges, traversal_profile, max_body_chars, entity_type, name, slug, summary, why_in_kb, tags, connections, affiliation, relationship, domain, language, repo, license, used_in, decided, project, decision_status, aliases, ref, expected_hash, why, expected_fingerprint, vocabulary_ref, vocabulary_fingerprint, identity_decision, facets, entity_family, registry_scope, relations, response_detail | Find relations, graph context or entities. |
| adopt_vault | 1 | MCP, REST, CLI | write | no | path | adopt | path, mode, max_depth, include_hidden, samples, pack_limit, manifest_path, selected_paths, semantic_max_files, semantic_max_bytes, semantic_example_limit, response_detail | Scan or import; preserve originals. |
| adoption_studio | 1 | MCP, REST, CLI | write | yes | - | adopt | action*, run_id, path, include_hidden, initialize_kb, collection, include, exclude, overrides, include_junk, plan_id, continuation, retry_failed, only_paths, why, write_manifest, sources, max_sources, max_chars_per_source, proposals, ref, expected_fingerprint, expected_hash, response_detail | Review and import an existing collection of notes, documents, images, audio and video in a resumable run. |
| maintain_memory | 1 | MCP, REST, CLI | write | yes | - | audit, audit_fix, reconcile | mode, categories, dry_run, rebuild_embeddings, rebuild_graph, detail, legacy_sample_limit, collection, apply, plan_id, source_snapshot, why, curation_action, run_id, plan, refs, paths, review_ref, hydration_recheck, expected_plan_fingerprint, vocabulary_ref, vocabulary_fingerprint, exclude_groups, acknowledge_skipped, response_detail | Maintain vault health; several modes write. |
| schema_memory | 1 | MCP, REST, CLI | write | yes | - | schema_memory | operation*, name, subject, project, page_type, save, expected_hash, strict, compare_to, proposal, why, include_model_suggestions, context, date_from, date_to, continuation, limit, query, requested_type, vocabulary_ref, vocabulary_fingerprint, detail, version, registry_scope, response_detail | Infer, validate, diff, or save governed memory schemas and workflow contracts. |
| govern_memory | 2 | MCP, REST, CLI | write | yes | operation | - | operation*, session_action, documents, selector_paths, intent, ttl_seconds, target_ceiling, duration, proposal_id, scope, grant_id, scope_ids, audience, ceiling, token, authorization_session, purpose, duration_seconds, rule_ids, path, paths, backfill_action, companion_input, vocabulary_request_id, raw_release, response_detail | Manage opt-in confidential access policy. |
| manage_memory_file | 2 | MCP, REST, CLI | write | yes | - | create_file, list_directory, move_file, delete, append_to_file, list_trash, recover_from_trash | operation, path, content, frontmatter, overwrite, allow_curated, kind, parents, recursive, include_hidden, old_path, new_path, source_kind, domain, reason, update_wikilinks, confirm, force_orphan, force_superseded, expected_dead_inbound, trash_path, restore_path, date, validate_only, draft_id, draft_hash, draft_token, semantic_transition_token, relation_disposition, relation_review_hash, relation_review_reason, promotion_reason, response_detail | Manage files when typed tools do not fit. |
| record_memory | 1 | MCP, REST, CLI | write | yes | - | record_memory | action*, collection, manifest_path, manifest_text, why, scaffold, view, filters, columns, sort_by, descending, limit, aggregate, date_from, date_to, date_column, expand_children, expand_child, continuation, include_agent_history, output_format, item, item_key, expected_container_hash, expected_manifest_hash, acknowledged_gap_codes, body, delivery, changes, expected_item_version, refresh_presentation, held, hold, rows, source, on_reject, import_request, query, response_detail | Read or update observed state; Planning uses plan_memory; originals use Sources/Evidence. |
| plan_memory | 1 | MCP, REST, CLI | write | yes | - | plan_memory | action*, collection, manifest_path, manifest_text, why, scaffold, view, filters, columns, sort_by, descending, limit, aggregate, date_from, date_to, date_column, lifecycle, hierarchy_mode, hierarchy_depth, hierarchy_limit, continuation, include_agent_history, output_format, item, plan_id, expected_manifest_hash, expected_container_hash, acknowledged_gap_codes, body, changes, transition, expected_item_version, response_detail | Manage intent; record_memory holds outcomes. |
| query_dataset | 2 | MCP, REST, CLI | read | no | path | query_data | path*, record_path, filters, columns, sort_by, descending, limit, offset, aggregate, date_from, date_to, date_column | Query a vault CSV, TSV or JSON dataset. |
| read_media | 2 | MCP | read | no | path | get_video_frames | path*, max_frames, start_sec, end_sec | Read sampled video images inline (MCP only). |
| observe_transfer | 1 | MCP (Cloud only) | read | no | - | - | action, import_id | Check or start moving material into or out of this Exomem Cloud memory. |
| transfer_memory | 1 | MCP (Cloud only) | write | yes | - | - | action*, import_id, export_id, confirm | Cancel or discard an import, or request an export of this Exomem Cloud memory. |

## Semantic Authoring Command Contract

These registry descriptions are projected into MCP, REST/OpenAPI, and CLI parameter help.

Contract identity: `exomem.semantic-authoring:v9 sha256:a6978ee9249f3c3fac17339b9b92dc18be3c82e1206441725f6c2a750d984090`

### `create_note`

Semantic authoring [exomem.semantic-authoring:v9 sha256:a6978ee9249f3c3fac17339b9b92dc18be3c82e1206441725f6c2a750d984090]: Choose homes by future use and accumulated content, not mentions or broad scope. Keep coherent long notes; reuse or create a home for independent material. Live compiled creates, replacements and transitions to live need a valid nonempty semantic unit; pending pages may remain unit-free. A post-activation compliant page cannot lose its final valid semantic unit. Inspect affected claims and evidence. Distinguish attributed accounts from established facts. Correct suitable existing units with guarded writers, justified verdicts and exact links; verify current state. Refutation alone changes neither lifecycle nor rank. Writers retain prior interpretations; request bootstrap with profile=full for history guidance. Compact: `## Observations` then `- [category] content #tags (context) ^anchor`; one open category. Rich: `## <Governed Kind>`. Choose exactly one primary category: prefer a meaningful epistemic or operational role and put the domain in tags, but if the role would only be a generic fact, finding, or observation and the domain is the durable lens, use a domain category instead. Role example: - [decision] Relocate to a coastal city next spring #life ^relocation `missing_semantic_unit`: Add `## Observations` and `- [operating constraint] Keep retries bounded #reliability`. `empty_rich_unit`: Add substantive body content or remove the empty recognized heading. Keys/examples: call bootstrap(profile="full").

### `supersede_note`

Semantic authoring [exomem.semantic-authoring:v9 sha256:a6978ee9249f3c3fac17339b9b92dc18be3c82e1206441725f6c2a750d984090]: Choose homes by future use and accumulated content, not mentions or broad scope. Keep coherent long notes; reuse or create a home for independent material. Live compiled creates, replacements and transitions to live need a valid nonempty semantic unit; pending pages may remain unit-free. A post-activation compliant page cannot lose its final valid semantic unit. Inspect affected claims and evidence. Distinguish attributed accounts from established facts. Correct suitable existing units with guarded writers, justified verdicts and exact links; verify current state. Refutation alone changes neither lifecycle nor rank. Writers retain prior interpretations; request bootstrap with profile=full for history guidance. Compact: `## Observations` then `- [category] content #tags (context) ^anchor`; one open category. Rich: `## <Governed Kind>`. Choose exactly one primary category: prefer a meaningful epistemic or operational role and put the domain in tags, but if the role would only be a generic fact, finding, or observation and the domain is the durable lens, use a domain category instead. Role example: - [decision] Relocate to a coastal city next spring #life ^relocation `missing_semantic_unit`: Add `## Observations` and `- [operating constraint] Keep retries bounded #reliability`. `empty_rich_unit`: Add substantive body content or remove the empty recognized heading. Keys/examples: call bootstrap(profile="full").

### `observe_memory`

Semantic authoring [exomem.semantic-authoring:v9 sha256:a6978ee9249f3c3fac17339b9b92dc18be3c82e1206441725f6c2a750d984090]: Choose homes by future use and accumulated content, not mentions or broad scope. Keep coherent long notes; reuse or create a home for independent material. Live compiled creates, replacements and transitions to live need a valid nonempty semantic unit; pending pages may remain unit-free. A post-activation compliant page cannot lose its final valid semantic unit. Inspect affected claims and evidence. Distinguish attributed accounts from established facts. Correct suitable existing units with guarded writers, justified verdicts and exact links; verify current state. Refutation alone changes neither lifecycle nor rank. Writers retain prior interpretations; request bootstrap with profile=full for history guidance. Compact: `## Observations` then `- [category] content #tags (context) ^anchor`; one open category. Rich: `## <Governed Kind>`. `missing_semantic_unit`: Add `## Observations` and `- [operating constraint] Keep retries bounded #reliability`. `empty_rich_unit`: Add substantive body content or remove the empty recognized heading. Keys/examples: call bootstrap(profile="full").

### `edit_note`

Semantic authoring [exomem.semantic-authoring:v9 sha256:a6978ee9249f3c3fac17339b9b92dc18be3c82e1206441725f6c2a750d984090]: Choose homes by future use and accumulated content, not mentions or broad scope. Keep coherent long notes; reuse or create a home for independent material. Live compiled creates, replacements and transitions to live need a valid nonempty semantic unit; pending pages may remain unit-free. A post-activation compliant page cannot lose its final valid semantic unit. Inspect affected claims and evidence. Distinguish attributed accounts from established facts. Correct suitable existing units with guarded writers, justified verdicts and exact links; verify current state. Refutation alone changes neither lifecycle nor rank. Writers retain prior interpretations; request bootstrap with profile=full for history guidance. Compact: `## Observations` then `- [category] content #tags (context) ^anchor`; one open category. Rich: `## <Governed Kind>`. `missing_semantic_unit`: Add `## Observations` and `- [operating constraint] Keep retries bounded #reliability`. `empty_rich_unit`: Add substantive body content or remove the empty recognized heading. Keys/examples: call bootstrap(profile="full").

### `manage_memory_file`

Semantic authoring [exomem.semantic-authoring:v9 sha256:a6978ee9249f3c3fac17339b9b92dc18be3c82e1206441725f6c2a750d984090]: manage_memory_file create, overwrite, and append receive the same semantic precommit contract on the complete resulting compiled Markdown; prefer create_note or supersede_note when their typed route fits. Choose homes by future use and accumulated content, not mentions or broad scope. Keep coherent long notes; reuse or create a home for independent material. Live compiled creates, replacements and transitions to live need a valid nonempty semantic unit; pending pages may remain unit-free. A post-activation compliant page cannot lose its final valid semantic unit. Inspect affected claims and evidence. Distinguish attributed accounts from established facts. Correct suitable existing units with guarded writers, justified verdicts and exact links; verify current state. Refutation alone changes neither lifecycle nor rank. Writers retain prior interpretations; request bootstrap with profile=full for history guidance. Compact: `## Observations` then `- [category] content #tags (context) ^anchor`; one open category. Rich: `## <Governed Kind>`. `missing_semantic_unit`: Add `## Observations` and `- [operating constraint] Keep retries bounded #reliability`. `empty_rich_unit`: Add substantive body content or remove the empty recognized heading. Keys/examples: call bootstrap(profile="full").


## Hand-registered MCP Tools

`HAND_REGISTERED_EXCEPTIONS` lists product tools that cannot be generated by the generic MCP registry loop.
The default product surface currently has no hand-registered MCP exceptions.
Direct client file handles use `preserve_artifacts`; `transfer_artifact` remains the compatibility upload/download transport.

## Notes

- A `*` suffix in the parameter list means the parameter is required.
- Routes name implementation leaves. Previous public spellings remain callable; discovery recommends preferred spellings.
- Tier 2 commands are advanced governance administration and file/data operations exposed only when the surface enables tier 2.
- Destructive commands are writes that can replace, move, delete, or bulk-fix content.
