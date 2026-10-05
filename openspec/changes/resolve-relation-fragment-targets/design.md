## Context

Three pieces already exist and are not connected to each other.

**The fragment survives resolution.** `vault.normalize_wikilink` returns
`Knowledge Base/<rest>` with `.md` stripped and `#anchor` preserved — its
docstring says so and its implementation splits the anchor off, canonicalizes the
path, and re-appends it.

**The fragment is then discarded.** Two sites in `epistemic_graph` turn an
authored relation target into an edge. A note-level `## Relations` bullet goes
through `_relation_line_edges`, and a rich-unit `- relations:` row through
`_edges_for_page`. Both end in `_with_md`, whose second line is
`cleaned.split("|", 1)[0].split("#", 1)[0]`, and the rich-unit site splits the
`#` off before it even normalizes. The page is what reaches `_file_key`. No
warning is emitted, because from `_with_md`'s point of view nothing went wrong.

**The destination it should have used is already indexed.** `graph_nodes` carries
`unit_ref` with `idx_graph_nodes_unit_ref`, a unit node's key is
`"unit:" + sha256(unit_ref)`, and `_current_unit_status(conn, vault_root, unit_ref)`
resolves `parent#fragment` into `found` / `missing` / `ambiguous` / `stale` with
parent paths and drift counts. That function exists for reference validation; it
is the same question a fragment target asks.

## Goals / Non-Goals

**Goals.** A relation target may address a unit. An author who writes a precise
target gets a precise edge or an explicit reason why not. `shared_open_question`
proposes `duplicates` between the two units that actually share the question.

**Non-Goals.** No new relation vocabulary. No change to the source side, which is
already unit-precise. No fragment on a `links_to` edge from an ordinary inline
wikilink — those are untyped by construction and a unit-level untyped edge would
multiply the graph without adding a claim. No ranking or scoring.

## Decisions

**Degrade to the page, never drop the edge.** An unresolvable or ambiguous
fragment produces the page-level edge today's code produces, plus a diagnostic.
Refusing the edge would make a typo silently delete a relation, which is worse
than the imprecision being fixed. This mirrors `normalize_wikilink(strict=False)`,
which returns the cleaned input with a warning rather than raising.

**`ambiguous` and `missing` are one outcome for the edge and two for the
author.** The edge is page-level either way. The diagnostic distinguishes them,
because the remedies differ: a missing fragment is a typo or a moved unit, an
ambiguous one needs the target page to give its units distinct identity.

**One resolver owner, with the per-page read extracted from it.**
`_current_unit_status` resolves a unit reference to its parent paths, and its
per-page body (read the page's current bytes, parse, resolve the unit exactly)
is now `_current_page_unit`, which it and the relation edge builder both call.
The question is the same one -- does this page carry exactly one unit by that
name -- and two resolvers would disagree on `ambiguous`. A fragment goes through
`SemanticUnitDocument.resolve_fragment`, which builds the exact reference and
calls `resolve_unit`, so an authored anchor or a `unit-<fingerprint>` identity
resolves and nothing else does: no title, case or similarity matching.

**Resolve from the target's bytes, not the sidecar.** `_edges_for_page` has no
connection, and `graph_parent_refs` can be unpopulated mid-rebuild, so the
fragment is resolved from disk exactly as `_current_unit_parent_paths` already
does. Only the page's own bytes decide whether the fragment names one unit, and
the destination key is derived from the same parse that produced the node.

**A target edit re-derives its fragment dependants.** A unit destination is a
function of the target page's bytes: a rich unit's key hashes its body, and an
edit can add, remove or duplicate an anchor. Drain and the topology refresh
already widen to every page whose body links a changed page, through
`_dependency_sources_for_keys`. The live incremental refresh widened only when
resolver topology moved, so it now also widens to pages whose raw dependencies
carry a `#fragment` on a changed page, from the same persisted dependency rows.

**The author is told through the census and the edge.** An edge whose fragment
did not resolve carries `fragment_resolution` (`missing` or `ambiguous`) and the
authored `target_fragment`, beside the existing `target_resolution`. The
relation census counts them as `unresolved_fragment_edges` and
`ambiguous_fragment_edges` beside `unresolved_target_edges`. A write-response
advisory is not part of this change.

**The graph schema version bumps.** Edges already stored for `[[T#Unit]]` are
page-level; only a rebuild re-derives them, so `SCHEMA_VERSION` moves to 12.

**The contract version bumps.** The relation grammar gains a documented target
form, so `semantic_authoring`'s contract version and digest move, and with them
the scaffold skill headers and the pinned tool schemas. This is the change that
the archived design meant by "owns the schema bump" and it is not separable: an
authoring form nobody is told about is not an authoring form.

## Risks / Trade-offs

**The plugin fingerprint moves while one is already pending.**
`deploy/chatgpt/personal-plugin-contract.json` has held a
`pending_tool_surface_sha256` different from `registered_tool_surface_sha256`
since 0.45.0 (2026-08-11). This change makes that pending hash move again rather
than resolve. It does not create the unverified state and cannot clear it —
clearing it is an operator action against the live connector.

**Write-path latency.** Every relation carrying a fragment costs one unit lookup
against an indexed column. The bound has to be measured on the write path, not
argued: `tests/test_latency_gate.py` at 2k and 8k, reported in this change.

**A unit-level edge changes traversal fan-out.** Consumers that assume every
edge endpoint is a page — traversal profiles, `graph-find-ranking`, the
acceptance queue — need checking rather than assuming. The graph already holds
unit *nodes*, so the shape is not new, but a unit appearing as a *destination*
is.

**`duplicates` between units is a stronger claim than `relates_to` between
pages.** The existing `shared_open_question` candidate is deliberately weak
because it can only say the pages are related. Making it unit-precise makes the
proposal sharper and a false positive more costly; the acceptance queue stays the
gate, and the candidate stays a proposal.
