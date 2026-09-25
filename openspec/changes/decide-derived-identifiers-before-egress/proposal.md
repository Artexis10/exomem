## Why

The release plane decides a page when a surface names it in a field the plane knows, or when a leaf decides its own hits. Several derived structures work differently: they carry a page identifier in fields the terminal entry filter did not inspect, or they compute their answer over the whole vault and leave the release decision to the end. A relation proposal names its target in `to`, a graph edge names its endpoint as a `file:` node key, a tension pair names its members in `a` and `b`, and an evolution timeline names its anchor and head. A proposal list, a context pack, a timeline, an entity lookup or a directory listing that was built with a withheld page in it still reflects that page after the page itself is removed. Writes resolve a writer's links over every page, and write doors change their target without deciding it.

A restricted caller's answer should read as if the pages withheld from it were absent. The owner's answer should not change.

## What Changes

- The terminal entry filter decides the derived identifier fields (`to`, `from`, `a`, `b`, `topic_anchor`, `chain_id`, `src_key`, `dst_key`), any key whose name marks an identifier, `file:` node keys and vault URIs, extensionless page references, and the wikilinks in a proposed relation bullet. An entry naming a page the caller may not see is dropped whole.
- For a caller other than the owner, derived structures decide their candidates before they assemble, cap, rank or count them: `connect_memory` `context` and `graph-context` (seeds, packed pages, the graph walk and its edges), evolution timelines (anchors, heads and chain members), entity identity (`resolve-entity`, `create-entity`), and directory listings and overview totals.
- A writer other than the owner resolves its links over the pages it may see: a stem, title or path that matches only withheld pages resolves, warns and lists as it would if they were absent.
- Write doors decide their target before resolving or changing it. A target the writer may not see answers exactly as a missing one and is never read or changed.
- For a reader other than the owner, links whose candidates include a withheld page resolve as a vault without it would resolve them, lazily and only where a request looks: graph context and context walks, inbound links and pack neighbours; the page provenance strip lists a bare link as the absent twin does.
- Counts and ranks are computed after filtering. Whole-vault aggregates (audit, registries inferred from the corpus, activation and relation-queue coverage) are served to the owner only under a governed policy; other audiences receive `available: false` with `reason: "audience_restricted"`. Recall diagnostics computed before release decisions are not returned to a restricted caller, and its recall runs without the graph lane. Relation proposals, the relation queue and relation triage and acceptance are the owner's under a governed policy.
- Activation resolves a restricted caller's turn over the anchors it may see, omits L0 material silently (abstaining as `unresolved`), and does not return the vault freshness key to it.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-gate`: derived identifiers, derived structures, write-time link resolution and write doors are decided for the caller before they are emitted or acted on.
- `context-activation`: anchors resolve over a restricted caller's view, L0 material is omitted without a marker, and such a caller's packet is not cached.

## Impact

- Affected code: `governance/egress.py` (entry fields, `restricted_release_filter`, `visible_page_filter`, `write_target_withheld`, `guard_graph_context` edge endpoints), `memory_context.py`, `epistemic_graph.py` (`graph_context`, `suggest_relations`, the relation review batch), `relation_queue.py`, `evolution.py`, `attention.py`, `context_pack.py`, `link_summary.py`, `list_inbound_links.py`, `find.py`, `working_set.py`, `working_set_resolve.py`, `working_set_runtime.py`, `entity_candidates.py`, `list_directory.py`, `overview.py`, `vault.py` (`normalize_wikilink`, `resolve_under_vault`), `note.py`, `link.py`, `semantic_contract.py`, `capture_sweep.py`, and the write doors in `edit.py`, `replace.py`, `move_file.py`, `delete_file.py`, `delete_directory.py`, `append_to_file.py`, `reclassify_source.py` and `commands.py`.
- Affected tests: `tests/test_derived_identifier_egress.py` is new. It builds twin vaults (none withheld, one colliding withheld page, one neutral withheld page) and requires the restricted answers to match, for the `external` audience and for a verified principal.
- Contract: no tool schema or description changes. A restricted caller sees fewer entries and fewer diagnostic fields, whole-vault aggregates answer it with `audience_restricted`, and a refusal it could already receive for a missing page now also covers a withheld one. The owner's answers are unchanged.
