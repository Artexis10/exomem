# Capabilities

This file is generated from `src/exomem/commands.py`.
Run `uv run python scripts/generate-capabilities.py` to refresh it.
Run `uv run python scripts/generate-capabilities.py --check` to verify it is current.

## Summary

- Registry commands: 29
- Tier 1 commands: 19
- Tier 2 commands: 10
- Registry-generated MCP commands: 28
- REST commands: 28
- CLI commands: 28
- Hand-registered MCP tools: mint_download_token, mint_upload_token, note

## Command Registry

| Command | Tier | Surfaces | Mode | Destructive | CLI positional | Parameters | Summary |
| --- | ---: | --- | --- | --- | --- | --- | --- |
| bootstrap | 1 | MCP, REST, CLI | read | no | - | profile, workflow | Return Exomem's versioned operating contract for generic MCP clients. |
| find | 1 | MCP, REST, CLI | read | no | query | query, types, projects, tags, speakers, file_types, exclude_file_types, limit, scope, mode, graph, rerank, prefer_compiled, prefer_active, prefer_used, pack, detail, include_timings | Search / find / look up / query / retrieve / recall pages in the Knowledge Base (KB vault): notes, sources, insights, failures, patterns, experiments, entities. Hybrid semantic + keyword search, read-only. Filters are AND'd; tag/project lists are OR'd within. |
| suggest_links | 1 | MCP, REST, CLI | read | no | - | path, draft_title, draft_body, limit, scope | Suggest existing KB pages a note should link to. Read-only. |
| add | 1 | MCP, REST, CLI | write | no | - | content*, source_type*, title*, url, tags, why_captured | Capture raw content as an immutable source page in the Knowledge Base. |
| audit | 1 | MCP, REST, CLI | read | no | - | categories | Audit / lint / health-check the Knowledge Base: find orphans, broken wikilinks, supersession gaps, stale unprocessed sources, and stale-review candidates. Read-only. |
| attention | 1 | MCP, REST, CLI | read | no | - | categories, limit | Your review queue: the one ranked list of what in the Knowledge Base needs your attention today. Read-only. |
| overview | 1 | MCP, REST, CLI | read | no | path | path, max_depth, include_hidden, samples | Bounded, read-only structure report of the vault (or a subtree). |
| adopt | 1 | MCP, REST, CLI | write | no | path | path, mode, max_depth, include_hidden, samples, pack_limit, manifest_path, selected_paths | Adopt / import an existing vault safely: scan first, preserve originals. |
| evolution | 1 | MCP, REST, CLI | read | no | query | query, limit, scope, projects, tags | How a conclusion CHANGED over time — the supersession history of a topic, as timelines. Read-only. |
| audit_fix | 1 | MCP, REST, CLI | write | yes | - | dry_run, rebuild_embeddings | Run audit + auto-apply safe fixes; propose-only for risky categories. |
| reconcile | 1 | MCP, REST, CLI | write | no | - | dry_run | Heal vault drift from out-of-band edits in one pass. |
| provenance_report | 1 | MCP, REST, CLI | read | no | - | tag, key, value, path | Trace provenance: scan note bodies for `<!-- key:value -->` tags — where an opinion/take/flag came from. Read-only. |
| propose_compilation | 1 | MCP, REST, CLI | read | no | - | sources*, suggested_title | Draft / scaffold a compiled note from unprocessed source(s) — what to compile next, drain the source backlog. Read-only. |
| get | 1 | MCP, REST, CLI | read | no | path | path*, frontmatter_only, include_history, links, include_raw | Read / open / fetch / load the full contents of a KB or vault page by path. Returns frontmatter + body (+ raw content on request). |
| edit | 1 | MCP, REST, CLI | write | yes | path | path*, why*, new_body, tags, old_string, new_string, replace_all, heading, section_position, edits, row_key, take, overwrite, field, value, allow_curated, expected_hash, validate_only | Lightweight in-place edit of a page (body, tags, a surgical snippet, |
| replace | 1 | MCP, REST, CLI | write | yes | old_path | old_path*, content*, note_type*, title*, reason, project, projects, sources, tags, status, severity, pattern_type, domain, started, duration, hypothesis, n, concluded, medium, recorded, published, host, editor, project_category | Supersede an existing compiled page with a new one. |
| link | 1 | MCP, REST, CLI | write | no | - | entity_type*, name*, summary*, why_in_kb, tags, connections, affiliation, relationship, domain, language, repo, license, used_in, decided, project, decision_status | Create a typed entity under Entities/<Folder>/<Name>.md. |
| preserve | 1 | MCP, REST, CLI | write | no | - | scope*, category*, filename*, content*, description | Capture a TEXT artifact to Evidence/<scope>/<category>/. |
| note | 1 | REST, CLI | write | no | - | content*, note_type*, title*, project, projects, sources, tags, status, severity, pattern_type, domain, started, duration, hypothesis, n, concluded, medium, recorded, published, host, editor, suggestions, project_category | Create a compiled note in the Knowledge Base. |
| query_data | 2 | MCP, REST, CLI | read | no | path | path*, record_path, filters, columns, sort_by, descending, limit, offset, aggregate, date_from, date_to, date_column | Tier 2: structured query over a CSV/JSON data file under the vault. |
| create_file | 2 | MCP, REST, CLI | write | no | path | path*, content, frontmatter, overwrite, allow_curated, kind, parents | Tier 2: write a file — or, with `kind="dir"`, create a folder — at an |
| list_directory | 2 | MCP, REST, CLI | read | no | path | path, recursive, include_hidden | Tier 2: list files and subfolders at a vault path. Read-only. |
| move_file | 2 | MCP, REST, CLI | write | yes | - | old_path*, new_path*, update_wikilinks, allow_curated | Tier 2: relocate a file, optionally rewriting inbound wikilinks. |
| delete | 2 | MCP, REST, CLI | write | yes | path | path*, confirm*, recursive, force_orphan, force_superseded, allow_curated, expected_dead_inbound | Tier 2: trash a file OR folder (auto-detected). Reversible — moves to |
| append_to_file | 2 | MCP, REST, CLI | write | no | path | path*, content*, allow_curated | Tier 2: append text to an existing file. |
| list_trash | 2 | MCP, REST, CLI | read | no | - | date | Tier 2: enumerate recoverable trash entries. Read-only. |
| recover_from_trash | 2 | MCP, REST, CLI | write | no | trash_path | trash_path*, restore_path, allow_curated | Tier 2: undo a delete_file/delete_directory. |
| list_inbound_links | 2 | MCP, REST, CLI | read | no | target | target* | Tier 2: find files whose wikilinks resolve to `target`. Read-only. |
| get_video_frames | 2 | MCP | read | no | - | path*, max_frames, start_sec, end_sec | View / analyze / look inside a vault video: sampled keyframes returned INLINE as images — no download round-trip. |

## Hand-registered MCP Tools

`HAND_REGISTERED_EXCEPTIONS` are intentionally not generated by the generic MCP registry loop.
`note` is also available through REST and CLI from the registry; the MCP registration is custom so it can expose live project-key guidance.
`mint_upload_token` and `mint_download_token` are MCP-only helpers for remote file transfer flows.

## Notes

- A `*` suffix in the parameter list means the parameter is required.
- Tier 2 commands are advanced file and data operations exposed only when the surface enables tier 2.
- Destructive commands are writes that can replace, move, delete, or bulk-fix content.
