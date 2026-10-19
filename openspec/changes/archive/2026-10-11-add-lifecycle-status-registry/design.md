# Design

## Context

See the proposal for scope and the lifecycle spec for consumer predicates.
Current activation, carry, currency, find and recurrence use different status-key sets.
Some differences are intentional: pending named carry is allowed, while pending activation is not.
The shared vocabulary foundation supplies loading, snapshot identity, governed deltas, history and restore.

## Goals / Non-Goals

**Goal:** classify raw page labels through a consistent operation basis, with immediate visibility and bounded disclosure.
Typed note enums remain S4b. Collection, Planning, job and receipt states retain their existing protocols.
This slice changes no catalogue schema and enables no real-vault consolidation.

## Decisions

### Reuse the existing registry

Add `lifecycle_statuses.py`, `packs/core/statuses.yaml`, and one `RegistrySpec` for `_Schema/statuses.yaml`.
Use the existing generic overlay grammar, loader, cache, writer, history and restore.
The concrete gap is the status adapter's closed class attribute and public canonical invariant.
An external policy service would duplicate the existing governed writer and cannot supply these local page predicates.
Maintenance consists of one adapter and pack, explicit parameter plumbing, and dependencies in existing caches.

Seed `active → live`, `draft/planned → pending`, `superseded → superseded`, `archived → retired`, and `dropped → abandoned` as pack data.
Keep the existing treatment of other labels until an admitted extension registers them.
Parse the five classes as a closed protocol enum, with a one-line C6 reason.
Absent or empty status retains the public live default, creates no debt, and needs no overlay admission.
Malformed non-string values retain the prior public-live selection fallback, without debt or overlay admission.
The raw value remains unchanged for existing structural validation; lifecycle selection does not make malformed frontmatter valid.
Parsed-page consumers pass the raw frontmatter value to preserve this distinction.
Existing graph, lexical and carry metadata coerce status to text and cannot distinguish a malformed scalar from an authored string with that spelling.
Those metadata-only paths retain this storage limitation; S3 adds no schema migration, rebuild or spelling heuristic to recover information that the index lost.

### Preserve public canonical meanings

Normalize pack keys and authored labels through the adapter's same function.
Resolve canonical keys before consulting aliases, replacement metadata or overlays.
The adapter rejects canonical removal, shadowing, class changes, deprecation and redirection during save, parse and restore.
It also rejects extension key, label or alias collisions with canonical pack keys.
Restore uses the existing tolerant reader and rejects its error findings before publication.
Valid retained legacy bytes keep their reader meaning and restore exactly; in-place meaning rules remain exempt during restore.
This prevents invalid history from replacing usable configuration, while preserving legitimate recovery.
Generic `immutable={"attributes.class"}` alone is insufficient because generic deprecation can redirect an entry.

This limits configuration deliberately: an extension introduces distinct meaning instead of repurposing a public canonical label.
Requiring whole-overlay admission for canonical labels would disable ordinary behavior when only private extensions are hidden.

### Pass one operation basis explicitly

Root-bearing find, activation, compiler, recurrence and semantic-state boundaries create the classification basis.
It holds the public pack and, only when extension resolution is needed, one admitted effective snapshot.
Check `vocabulary.contract.admission_refusal` before any extension load or cached snapshot access.
Pass the basis or resolved class to rootless helpers; add no ambient context variable or second cache.
Retain raw labels in catalogues and transient resolved facts in `SemanticPageState`.

Canonical-only results depend on the public pack identity.
Extension-dependent results depend on their admitted effective digest and existing caller authority basis.
Unknown labels become live with debt only after admitted resolution; unavailable classification is a distinct state.
Do not publish an effective digest, private reason or debt from a denied extension lookup.

### Update consumers and freshness at their existing boundaries

Replace activation and semantic minimum lists, carry retirement lists, currency history lists and find penalties with class predicates.
Include mutual supersession qualification, unit-read disposition, and currency's active guard in the source audit.
Migrate each page-lifecycle comparison or document the separate protocol that requires its literal value.
Carry unit conversion uses the existing `parent_status` rather than synthesizing active status.
Preserve same-page supersession as a separate authored fact and keep current ranking magnitudes and candidate bounds.
Recurrence excludes superseded, retired and pending classes; abandoned evidence retains the former dropped-status behavior.
Planned evidence consequently stops supplying spread or anchors. Existing entity-tree exclusions and identity rules stay unchanged.

Bind page and unit find caches, compiler packet reuse and semantic census reuse to their actual classification dependency.
Derive the overlay path from the registry spec rather than copying its name into another dependency list.
Keep raw catalogue lifecycle labels; registry changes do not require an index rebuild.
Reuse existing save/restore and watcher invalidation, including foreign-process metadata checks on extension load.

### Keep carry rarity on its admitted catalogue snapshot

Extend the existing lexical query owner under `_serve_from_ready_catalog_result` and its single SQL read transaction.
Enumerate only paths in scope, excluding raw material, then apply the existing caller predicate before reading status or counting.
The denominator includes admitted navigation and retired pages; raw material contributes to neither numerator nor denominator.
Classify only admitted FTS contributors, so unrelated or hidden unfamiliar labels cannot trigger extension lookup.
Return frequencies and bounded discount paths from that snapshot; preserve the current pointer-read cap and fresh pointer reads.
Pass the admitted paths into existing BM25 ranking before its limit, preserving ambiguity and candidate bounds.
Restricted denominators require N scalar path admissions, paid only when a carry query has usable stems and needs rarity.
Keep those results request-local; add no global status census, projector or frequency cache.
An omitted predicate asks the existing walk admission owner; an owner label or custom predicate alone never proves completeness.
Measure the whole stage at the existing production-volume profile without changing its budget.

Page edit, replacement and adoption guards reject the superseded class through their existing errors; unavailable classification refuses the dependent operation.
Relation debt, provenance and due-state audits use the live class; stale review retains live and abandoned evidence.
Contradiction candidates retain their separate exclusion of historical classes; referent anchors permit live and pending classes.
Entity identity and Planning, Records, jobs and receipt states keep their existing protocols.

### Separate shared structure from admitted lifecycle facts

The shared semantic corpus cache, uncached builder and delta builder retain neutral parsed state and the complete stable identity census.
One enrichment step admits paths before classifying statuses and produces a detached request context; it never updates shared cache entries or flights.
A caller who admits every page reuses its enrichment through a separate memo, keyed by the structural cache entry and bound to the status dependency it consulted. Restricted callers never read or write that memo, and the shared structural entries stay neutral.
Candidate validation requires classification before applying minimum-unit exemptions.
A stored activation manifest uses the target's admitted compiled eligibility, content hash and identity, plus complete identity ownership evidence.
Duplicate identities anywhere in that complete census cannot inherit a stable-identity exemption, including duplicates on otherwise ineligible pages.
Explicit request paths never replace missing identity evidence with an implicit activation scan.

Activation snapshots remain separate complete evidence.
The activation census is lifecycle-neutral and caller-independent: the shared corpus walk records each structurally eligible compiled page and its raw status label, without classifying it.
A filtered request view never supplies the census, so a partial view cannot grandfather the wrong corpus, and the census is never served.
A write prepares an absent manifest from that complete neutral census, whoever the caller is; its response depends only on the admitted target.
Manifest preparation records the owner's class at activation next to each recorded label, classified by the owner-local producer in its own boundary, so a page that is not live at activation is never grandfathered even if its label is redirected later.
A grandfathering check classifies the recorded label with the caller's basis before it reads that class, so a caller without the owner's definitions gets the same unavailable result whatever class the owner recorded.
A label with no available class at preparation is classified at check time only.
The manifest records a page's status label only when the shipped pack cannot resolve it, so a vault that uses only pack labels keeps the previous release's manifest bytes; an older release cannot read a manifest that records a label.
A grandfathering exemption classifies the target's recorded label with the caller's admitted basis; an unadmitted label on the target makes the exemption unavailable.
Commit preparation never interprets a missing census as permission to discover the whole corpus; the shared walk supplies it.
The shipped pack holds every status label the product itself writes, so canonical product labels never depend on vault definitions.
Fast validity-token reuse requires fresh complete-view authority and current status dependencies; other callers use existing warm preflight revalidation.

Dreamer queries keep their graph snapshot, ordering and existing 64-row contributor limits.
They stream candidates, admit paths before classification, and count only eligible rows toward each limit.
Current parsed-page eligibility replaces serving predicates on the stored `review_eligible` bit.
The background producer now records structural eligibility only, without status admission or overlay reads.
Serving code ignores older, narrower bits; no graph schema change or rebuild is required.
This can examine and parse more candidate pages before reaching the same cap, especially with many hidden or retired predecessors.
Pure invalidation queries may cover a larger path superset, without serving caller content.

### Classify other derived consumers at their serving boundary

`relation_review_batch` retains the existing aggregate admission and current graph snapshot.
It streams file metadata in priority/path order and applies the shared structural predicate and one admitted status basis.
Coverage counts every eligible source; only the first `source_cap + 1` sources enter existing bounded candidate-family queries.
The aggregate now examines O(N) file metadata, including existing identity counts, rather than claiming fixed query work.
A missing status dependency remains unavailable through the shared error envelope; it is not warming or zero coverage.
Graph metadata still relies on existing publication freshness. Dreamer combines that snapshot with current admitted page parsing, without adding a new freshness owner.

Artifact-role descriptors retain raw status and structural eligibility only.
Serving recomposes admitted origin and support eligibility with the current basis; unavailable meaning produces unknown coverage.
Old stored eligibility flags cannot serve as current lifecycle proof, and no registry-triggered index rebuild is added.
Vocabulary projection keeps its physical pagination, cursor and query budgets, then classifies freshly read admitted endpoints before emitting pairs.
Its empty-for-write shortcut uses the status-neutral candidate superset, so any possible row declines the shortcut without private classification.

New retained-input bindings reject class `superseded` after existing page release.
Prior-binding disclosure still depends on release and exact unit identity, not current parent disposition.
One internal reader keyword suppresses lifecycle disposition only for this disclosure path; `found` there means the retained unit resolves.
The episode recorder's `superseded` tombstone remains a closed atomic revision token in `add` and `episode_memory`.
Entity identity and Planning, Records, job and receipt states remain separate protocols.

### Keep dependent unavailability honest

A denied extension can stop status-based ranking, current/history selection, or a mutation that needs minimum-unit classification.
Use the existing shared error envelope for a dependent find or write; do not describe authority unavailability as index warming.
Find with status preference disabled and independently admitted path reads remain available.
Compiler components use their existing unavailable/abstained state without an inferred active default.
This prevents private extension meanings from changing visible claims. A false refusal costs only the caller's extension-dependent operation.
The caller pays that cost until admission changes; T16 owns finer private vocabulary domains.

### Version normative authoring separately

Revise the live semantic contract to teach the fixed `live` class rule and status-registry route.
Advance its version and digest through the existing producer; keep `AUTHORING_CONTRACT` immutable and vault-independent.
Retain historical hosted descriptors and authoring artifacts; shared runtime semantics follow the current release.
Version the existing bootstrap operating contract with separate corrective guidance, following the source-capture migration precedent.
That guidance supersedes historical inactive-label teaching for shared validation without changing the historical semantic contract's version or digest.
Teach the live-class rule, public statusless default, admitted unknown debt, and unavailable classification's dependent refusal.
The correction must survive command filtering and appear in compact and full bootstrap without requiring the newer `section` parameter.
Frozen profiles v1–v3 expose full bootstrap but no registry inspection tool; do not advertise an unavailable inspection call.
Frozen v4 exposes `schema_memory` with string subject and operation; advertise its actual status-inspection route through the existing surface filter.
Prove a v3 full-bootstrap and `edit_memory` validation journey after a real owner registry save changes an extension's class.
Keep any admitted live definitions and dependency identity separate from normative fields and static corrective guidance.
Compose S2's lookup guidance through the existing renderers, stamps and packaging path at integration.

## Risks / Trade-offs

- Warm caches can preserve obsolete eligibility: bind each classification-dependent reuse to its actual registry basis.
- Private extension aliases can disclose definitions: admit before loading and prove denied absent/present/alias equivalence.
- Planned evidence can remove a recurrence finding: prove the threshold case and record this intended recall cost.
- Canonical meanings cannot be repurposed: use a distinct extension key, preserving compatibility and private-independent behavior.
- An older release treats extension labels as unknown: retain the overlay and history, and report changed classification during rollback.

## Migration Plan

1. Integrate the actual merged foundation and accepted S2 guidance; preserve their verified contracts.
2. Ship the adapter, pack and consumers through ordinary review and release; existing page bytes remain unchanged.
3. Restore the previous registry version when reverting an extension; removed labels remain live with debt after admitted resolution.
4. Roll back the release through the existing procedure; keep the overlay and retained history for later recovery.

## Evidence

These numbers record what the delivered source costs. They make no claim of a gain or a loss against an earlier release.

Each measurement ran on the source of commit `019d0d77eb09765d366ba37f79e8d8bf5e912972`, with a scratch instrument that is not in the repository.
A reader cannot rerun these numbers from the repository alone; each section names the repository test whose scenario the instrument rebuilt.
The instrument wraps the named functions with counters and `time.perf_counter`; it does not change their arguments or results.
The runs used the shared test wrapper with `EXOMEM_TEST_UMASK=022`, one measuring process at a time, on a laptop: Intel Core Ultra 7 255HX (20 logical CPUs), 23 GB RAM, WSL2 Linux 6.18, load average about 1.5.
Other sessions could run tests at the same time.
"Cold" means that the in-process caches were cleared; the operating-system file cache stayed warm.

### Carry rarity at production volume (task 2.6)

The instrument rebuilds the second state of `test_hidden_status_contributors_do_not_change_public_carry_at_production_volume`: 2,000 bulk notes, the carry pages, 70 withheld contributors and 3 retired revisions.
It then calls `op_activate_context` with the genuine turn as the `external` caller, cold and then warm, three times (n = 3 per state).
Before each cold call it clears the governance, find, packet, corpus-context and lexical-store caches.

Each activation ran one rarity stage (`rare_turn_terms`) with one SQL read transaction (`LexicalStore.carry_term_statistics`) over 9 turn terms.
That transaction examined 2,209 paths, admitted 2,139 and classified 4,217 matched rows in every run.

| Run | Rarity stage ms | SQL transaction ms | Admission predicate ms | Whole activation ms |
| --- | --- | --- | --- | --- |
| cold 1 | 3,062.7 | 3,062.5 | 3,036.5 | 5,232.4 |
| warm 1 | 658.8 | 658.7 | 638.9 | 1,488.2 |
| cold 2 | 3,088.9 | 3,088.8 | 3,063.2 | 3,891.7 |
| warm 2 | 658.3 | 658.2 | 637.5 | 1,400.6 |
| cold 3 | 3,338.9 | 3,338.8 | 3,311.8 | 4,157.9 |
| warm 3 | 1,017.3 | 1,017.2 | 994.2 | 1,894.6 |

The admission predicate took 96.9 to 99.2 percent of the SQL transaction in each run.

### Relation queue at 3,600 pages (task 2.7)

The instrument builds the fixture of `test_relation_queue_graph_native.py::test_queue_uses_one_fixed_cost_graph_batch_for_3600_pages` (3,601 eligible pages).
It then calls `relation_queue.build_queue(limit_pages=5, limit_per_page=5)` four times in one process (n = 4).

| Call | State before the call | Graph snapshot opens | SQL statements | `find._parse_page` calls | ms |
| --- | --- | --- | --- | --- | --- |
| 1 | graph just built, as in the test | 1 | 7 | 0 | 329.7 |
| 2 | find and corpus-context caches cleared | 1 | 7 | 3,601 | 3,241.4 |
| 3 | warm | 1 | 7 | 0 | 1,186.5 |
| 4 | warm | 1 | 7 | 0 | 1,154.3 |

No call reached `find_corpus.parse_page`.

### Hydration after a status registry save and restore (task 2.7)

The instrument runs the scenario of `test_a_status_registry_save_and_restore_change_hydration_without_page_rewrites` as the owner (n = 1).
It counts Markdown parses (`find._parse_page` and `find_corpus.parse_page`) and Dreamer page reads (`dreamer_families.Context.page`) in each step.

| Step | Markdown parses | Dreamer page reads | ms |
| --- | --- | --- | --- |
| Dreamer run to quiet | 0 | 77 | 120.1 |
| Serve the item before the save | 0 | 2 | 7.6 |
| Serve the item after the save; the proposal lapses | 0 | 2 | 6.4 |
| Dreamer run to quiet after the save | 0 | 0 | 21.8 |
| Serve the item after the restore | 0 | 2 | 7.5 |

The fixture's graph publish filled the parse cache, so no step parsed Markdown.
The save changed the served proposal without Dreamer page work; the serve-time revalidation read the two contributors.
