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
Candidate validation requires classification before applying minimum-unit exemptions.
A stored activation manifest uses the target's admitted compiled eligibility, content hash and identity, plus complete identity ownership evidence.
Duplicate identities anywhere in that complete census cannot inherit a stable-identity exemption, including duplicates on otherwise ineligible pages.
Explicit request paths never replace missing identity evidence with an implicit activation scan.

Activation snapshots remain separate complete evidence.
The activation census is lifecycle-neutral and caller-independent: the shared corpus walk records each structurally eligible compiled page and its raw status label, without classifying it.
A filtered request view never supplies the census, so a partial view cannot grandfather the wrong corpus, and the census is never served.
A write prepares an absent manifest from that complete neutral census, whoever the caller is; its response depends only on the admitted target.
Manifest preparation decides membership once, with the owner-local producer's status classes in its own boundary, so a page that is not live at activation stays out even if its label is redirected later.
A label with no available class at preparation falls back to classification at check time.
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
