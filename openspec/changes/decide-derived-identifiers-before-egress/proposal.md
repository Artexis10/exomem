## Why

The release plane decides a page when a surface names it in a field the plane knows, or when a leaf decides its own hits. Several derived structures work differently: they carry a page identifier in fields the terminal entry filter did not inspect, or they compute their answer over the whole vault and leave the release decision to the end. A relation proposal names its target in `to`, a graph edge names its endpoint as a `file:` node key, a tension pair names its members in `a` and `b`, and an evolution timeline names its anchor and head. A proposal list, a context pack, a timeline, an entity lookup or a directory listing that was built with a withheld page in it still reflects that page after the page itself is removed. Writes resolve a writer's links over every page, and write doors change their target without deciding it.

A restricted caller's answer should read as if the pages withheld from it were absent. The owner's answer should not change.

## What Changes

- The terminal entry filter decides the derived identifier fields (`to`, `from`, `a`, `b`, `topic_anchor`, `chain_id`, `src_key`, `dst_key`), any key whose name marks an identifier, `file:` node keys and vault URIs, extensionless page references, and the wikilinks in a proposed relation bullet. An entry naming a page the caller may not see is dropped whole.
- For a caller other than the owner, derived structures decide their candidates before they assemble, cap, rank or count them: `connect_memory` `context` and `graph-context` (seeds, packed pages, the graph walk and its edges), relation proposals and the relation queue (targets and the pages their evidence rests on), evolution timelines (anchors, heads and chain members), entity identity (`resolve-entity`, `create-entity`), and directory listings and overview totals.
- A writer other than the owner resolves its links over the pages it may see: a stem, title or path that matches only withheld pages resolves, warns and lists as it would if they were absent.
- Write doors decide their target before resolving or changing it. A target the writer may not see answers exactly as a missing one and is never read or changed.
- Planned in the same change: link resolution per audience for restricted callers, counts and ranks computed after filtering, and whole-vault aggregates served to the owner only under a governed policy.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-gate`: derived identifiers, derived structures, write-time link resolution and write doors are decided for the caller before they are emitted or acted on.

## Impact

- Affected code: `governance/egress.py` (entry fields, `restricted_release_filter`, `visible_page_filter`, `write_target_withheld`, `guard_graph_context` edge endpoints), `memory_context.py`, `epistemic_graph.py` (`graph_context`, `suggest_relations`, the relation review batch), `relation_queue.py`, `evolution.py`, `entity_candidates.py`, `list_directory.py`, `overview.py`, `vault.py` (`normalize_wikilink`, `resolve_under_vault`), `note.py`, `link.py`, `semantic_contract.py`, `capture_sweep.py`, and the write doors in `edit.py`, `replace.py`, `move_file.py`, `delete_file.py`, `delete_directory.py`, `append_to_file.py`, `reclassify_source.py` and `commands.py`.
- Affected tests: `tests/test_derived_identifier_egress.py` is new. It builds twin vaults (none withheld, one colliding withheld page, one neutral withheld page) and requires the restricted answers to match, for the `external` audience and for a verified principal.
- Contract: no tool schema or description changes. A restricted caller sees fewer entries, and a refusal it could already receive for a missing page now also covers a withheld one. The owner's answers are unchanged.
